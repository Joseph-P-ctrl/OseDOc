from __future__ import annotations

import html
import os
import re
import sqlite3
import subprocess
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

try:
  from pypdf import PdfReader
except Exception:
  PdfReader = None

WORKSPACE_DIR = Path(__file__).resolve().parent


def _load_dotenv(dotenv_path: Path) -> None:
  if not dotenv_path.exists() or not dotenv_path.is_file():
    return

  for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
      continue
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if key and key not in os.environ:
      os.environ[key] = value


_load_dotenv(WORKSPACE_DIR / ".env")

app = FastAPI(title="OsiDOc Viewer", version="1.0.0")

_DOWNLOADS_ENV = os.getenv("OSI_DOWNLOAD_DIR", "downloads")
DOWNLOADS_DIR = Path(_DOWNLOADS_ENV)
if not DOWNLOADS_DIR.is_absolute():
    DOWNLOADS_DIR = (WORKSPACE_DIR / DOWNLOADS_DIR).resolve()

_DB_ENV = os.getenv("OSI_SQLITE_PATH", str(DOWNLOADS_DIR / "notificaciones.db"))
DB_PATH = Path(_DB_ENV)
if not DB_PATH.is_absolute():
    DB_PATH = (WORKSPACE_DIR / DB_PATH).resolve()

PAGE_SIZE_FIXED = 10
REMOTE_CHECK_STALE_MINUTES = int(os.getenv("OSI_REMOTE_CHECK_STALE_MINUTES", "10"))
COLUMNS_TO_DISPLAY = [
    "nro__notificacion",
    "asunto",
    "fecha_de_notificacion",
    "fecha_importacion",
]

UPDATE_STATE = {
  "running": False,
  "progress": 0,
  "error": None,
  "message": "",
  "started_at": None,
  "recent_downloads": [],
}


def _run_update():
  """Ejecuta osinergmin_auth.py en thread separado."""
  try:
    UPDATE_STATE["running"] = True
    UPDATE_STATE["error"] = None
    UPDATE_STATE["message"] = "Iniciando actualización incremental..."
    UPDATE_STATE["progress"] = 10
    UPDATE_STATE["started_at"] = time.time()
    UPDATE_STATE["recent_downloads"] = []

    script_path = WORKSPACE_DIR / "osinergmin_auth.py"
    result = subprocess.run(
      [
        str(WORKSPACE_DIR / ".venv" / "Scripts" / "python.exe"),
        str(script_path),
        "--incremental-only",
        "--skip-existing-notifications",
      ],
      cwd=str(WORKSPACE_DIR),
      capture_output=True,
      text=True,
      timeout=600,
    )

    combined_output = (result.stdout or "") + "\n" + (result.stderr or "")
    moved_files = re.findall(
      r"Archivo movido\s+.*?:\s*(.+?)(?:\s+\(\d+\s+bytes\))?\s*$",
      combined_output,
      flags=re.MULTILINE,
    )
    unique_recent_downloads = list(dict.fromkeys([m.strip() for m in moved_files if m and m.strip()]))
    UPDATE_STATE["recent_downloads"] = unique_recent_downloads[:20]

    if result.returncode == 0:
      UPDATE_STATE["progress"] = 100
      if "No hay notificaciones nuevas o pendientes por descargar." in combined_output:
        UPDATE_STATE["message"] = "No hay nada nuevo para descargar."
      elif "No se descargaron documentos notificados." in combined_output:
        UPDATE_STATE["message"] = "No hubo documentos nuevos para descargar."
      elif unique_recent_downloads:
        UPDATE_STATE["message"] = f"Actualización completada. Se descargaron {len(unique_recent_downloads)} documento(s)."
      else:
        UPDATE_STATE["message"] = "Actualización completada."
    else:
      UPDATE_STATE["error"] = f"Proceso finalizado con código {result.returncode}"
      UPDATE_STATE["message"] = "La actualización terminó con error."
  except Exception as e:
    UPDATE_STATE["error"] = str(e)
    UPDATE_STATE["progress"] = 0
    UPDATE_STATE["message"] = "Error durante la actualización."
  finally:
    _build_notification_files_metadata.cache_clear()
    UPDATE_STATE["running"] = False
    UPDATE_STATE["started_at"] = None


@app.on_event("startup")
async def _auto_sync_on_startup():
    """Descarga automáticamente al abrir la app si la carpeta de hoy no existe todavía."""
    today_folder = DOWNLOADS_DIR / datetime.now().strftime("%Y-%m-%d")
    if not today_folder.exists() and not UPDATE_STATE["running"]:
        thread = threading.Thread(target=_run_update, daemon=True)
        thread.start()


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def _get_table_columns(con: sqlite3.Connection) -> list[str]:
    rows = con.execute("PRAGMA table_info(notificaciones)").fetchall()
    return [str(r["name"]) for r in rows]


def _find_notification_column(columns: list[str]) -> str | None:
    for col in columns:
        if "notif" in col.lower():
            return col
    return None


def _find_due_date_column(columns: list[str]) -> str | None:
  for col in columns:
    c = col.lower()
    if "venc" in c and "fecha" in c:
      return col
  for col in columns:
    if "venc" in col.lower():
      return col
  return None


def _normalize_text(value: str) -> str:
  normalized = unicodedata.normalize("NFD", value or "")
  without_accents = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
  return without_accents.lower().strip()


def _infer_document_type(text: str) -> str:
  t = _normalize_text(text)
  if not t:
    return "No identificado"

  # Prioridad alta: tipos mas especificos.
  if any(k in t for k in [
    "cobranza coactiva",
    "coactiva",
    "ejecutor coactivo",
    "ejecucion coactiva",
    "medida cautelar",
  ]):
    return "Cobranza coactiva"

  if any(k in t for k in [
    "requerimiento",
    "requerir",
    "se requiere",
    "subsanar",
    "cumplimiento",
    "plazo otorgado",
  ]):
    return "Requerimiento"

  if any(k in t for k in [
    "oficio",
    "carta",
    "of.",
    "oficio multiple",
  ]):
    return "Oficio"

  if any(k in t for k in [
    "informe",
    "informe tecnico",
    "reporte tecnico",
    "dictamen",
    "memorando",
  ]):
    return "Informe"

  if any(k in t for k in [
    "resolucion",
    "resuelve",
    "resolutiva",
    "apelacion",
    "recurso",
    "queja",
    "pronunciamiento",
    "sancion",
    "multa",
    "acto administrativo",
  ]):
    return "Resolucion"

  # Fallback para documentos administrativos sin palabra clave explicita.
  return "Resolucion"


def _extract_pdf_text(path: Path, max_pages: int | None = None, max_chars: int = 200000) -> str:
  """Extrae texto del PDF para inferir metadata de forma mas completa."""
  if PdfReader is None:
    return ""

  try:
    reader = PdfReader(str(path))
    parts: list[str] = []
    pages = reader.pages if max_pages is None else reader.pages[:max_pages]
    for page in pages:
      text = page.extract_text() or ""
      if text:
        parts.append(text)
    joined = "\n".join(parts)
    if len(joined) > max_chars:
      return joined[:max_chars]
    return joined
  except Exception:
    return ""


def _normalize_due_candidate(candidate: str) -> str:
  token = re.sub(r"\s+", " ", candidate.strip())
  token = token.replace(".", "/").replace("-", "/")

  m_num = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", token)
  if m_num:
    d, m, y = int(m_num.group(1)), int(m_num.group(2)), int(m_num.group(3))
    if y < 100:
      y += 2000
    try:
      return datetime(y, m, d).strftime("%d/%m/%Y")
    except ValueError:
      return ""

  month_map = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "setiembre": 9,
    "septiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
  }
  m_txt = re.match(r"^(\d{1,2})\s+de\s+([a-z]+)\s+de\s+(\d{4})$", token)
  if m_txt:
    d = int(m_txt.group(1))
    month_name = m_txt.group(2)
    y = int(m_txt.group(3))
    month_number = month_map.get(month_name)
    if month_number:
      try:
        return datetime(y, month_number, d).strftime("%d/%m/%Y")
      except ValueError:
        return ""
  return ""


def _extract_due_date(text: str) -> str:
  """Busca fecha de vencimiento contextual en texto libre y normaliza a dd/mm/yyyy."""
  raw = _normalize_text(text)
  compact = re.sub(r"\s+", " ", raw)

  months = r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|setiembre|septiembre|octubre|noviembre|diciembre"
  date_num = r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})"
  date_txt = rf"(\d{{1,2}}\s+de\s+(?:{months})\s+de\s+\d{{4}})"

  patterns = [
    rf"fecha\s+de\s+vencimiento\s*[:\-]?\s*{date_num}",
    rf"fecha\s+de\s+vencimiento\s*[:\-]?\s*{date_txt}",
    rf"vencimiento\s*[:\-]?\s*{date_num}",
    rf"vencimiento\s*[:\-]?\s*{date_txt}",
    rf"vence\s*(?:el)?\s*[:\-]?\s*{date_num}",
    rf"vence\s*(?:el)?\s*[:\-]?\s*{date_txt}",
    rf"plazo\s+(?:maximo\s+)?(?:hasta|vence)\s*(?:el)?\s*{date_num}",
    rf"plazo\s+(?:maximo\s+)?(?:hasta|vence)\s*(?:el)?\s*{date_txt}",
    rf"tiene\s+plazo\s+hasta\s+el\s*{date_num}",
    rf"tiene\s+plazo\s+hasta\s+el\s*{date_txt}",
  ]

  for pat in patterns:
    m = re.search(pat, compact)
    if not m:
      continue
    normalized = _normalize_due_candidate(m.group(1))
    if normalized:
      return normalized
  return ""


def _extract_deadline_days(text: str) -> int | None:
  """Extrae plazo en dias cuando no hay fecha de vencimiento explicita."""
  raw = re.sub(r"\s+", " ", _normalize_text(text))
  patterns = [
    r"plazo\s+de\s+(\d{1,3})\s+dias",
    r"en\s+el\s+plazo\s+de\s+(\d{1,3})\s+dias",
    r"dentro\s+de\s+(\d{1,3})\s+dias",
    r"cuenta\s+con\s+(\d{1,3})\s+dias",
    r"otorga\w*\s+un\s+plazo\s+de\s+(\d{1,3})\s+dias",
  ]
  values: list[int] = []
  for pat in patterns:
    for found in re.findall(pat, raw):
      try:
        day_count = int(found)
      except ValueError:
        continue
      if 1 <= day_count <= 365:
        values.append(day_count)
  return min(values) if values else None


def _parse_notification_date(value: str) -> datetime | None:
  token = (value or "").strip()
  if not token:
    return None

  formats = [
    "%d/%m/%Y %I:%M:%S %p",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
  ]
  for fmt in formats:
    try:
      return datetime.strptime(token, fmt)
    except ValueError:
      continue

  m_dmy = re.search(r"(\d{2}/\d{2}/\d{4})", token)
  if m_dmy:
    try:
      return datetime.strptime(m_dmy.group(1), "%d/%m/%Y")
    except ValueError:
      pass

  m_ymd = re.search(r"(\d{4}-\d{2}-\d{2})", token)
  if m_ymd:
    try:
      return datetime.strptime(m_ymd.group(1), "%Y-%m-%d")
    except ValueError:
      pass
  return None


def _to_datetime(date_text: str) -> datetime | None:
  try:
    return datetime.strptime(date_text, "%d/%m/%Y")
  except Exception:
    return None


@lru_cache(maxsize=1024)
def _build_notification_files_metadata(numero: str) -> list[dict[str, str]]:
  docs = sorted(DOWNLOADS_DIR.glob(f"*/{numero}/*"), key=lambda p: p.name.lower())
  files: list[dict[str, str]] = []
  for path in docs:
    relative = path.relative_to(DOWNLOADS_DIR).as_posix()
    href = f"/files/{quote(relative)}"
    date_folder = path.parents[1].name if len(path.parents) > 1 else ""
    try:
      size_kb = f"{path.stat().st_size / 1024:.2f}"
    except OSError:
      size_kb = "0.00"

    pdf_text = _extract_pdf_text(path)
    searchable = f"{path.name}\n{pdf_text}"
    doc_type_from_name = _infer_document_type(path.name)
    final_doc_type = doc_type_from_name if doc_type_from_name != "No identificado" else _infer_document_type(searchable)
    deadline_days = _extract_deadline_days(searchable)
    files.append(
      {
        "name": path.name,
        "href": href,
        "date_folder": date_folder,
        "size_kb": size_kb,
        "document_type": final_doc_type,
        "due_date": _extract_due_date(searchable),
        "deadline_days": str(deadline_days) if deadline_days is not None else "",
      }
    )
  return files


def _summarize_notification_metadata(numero: str, fallback_text: str = "", notification_date_text: str = "") -> tuple[str, str]:
  files = _build_notification_files_metadata(numero)
  due_dates = [f.get("due_date", "") for f in files if f.get("due_date")]
  parsed = [d for d in (_to_datetime(v) for v in due_dates) if d is not None]
  due_value = min(parsed).strftime("%d/%m/%Y") if parsed else ""

  if not due_value:
    notif_dt = _parse_notification_date(notification_date_text)
    days_candidates: list[int] = []
    for f in files:
      day_text = (f.get("deadline_days") or "").strip()
      if day_text.isdigit():
        days_candidates.append(int(day_text))
    if notif_dt and days_candidates:
      estimated_dt = notif_dt + timedelta(days=min(days_candidates))
      due_value = f"{estimated_dt.strftime('%d/%m/%Y')} (estimado)"

  types = [f.get("document_type", "") for f in files if f.get("document_type") and f.get("document_type") != "No identificado"]
  if types:
    doc_type = Counter(types).most_common(1)[0][0]
  else:
    doc_type = _infer_document_type(fallback_text)
  return due_value, doc_type


def _notifications_with_files(base_dir: Path) -> set[str]:
    out: set[str] = set()
    pat = re.compile(r"\d{8,}-\d+")
    if not base_dir.exists():
        return out

    for folder in base_dir.rglob("*"):
        if not folder.is_dir() or not pat.fullmatch(folder.name):
            continue
        try:
            has_file = any(p.is_file() for p in folder.iterdir())
        except Exception:
            has_file = False
        if has_file:
            out.add(folder.name)
    return out


def _get_pending_notifications() -> list[str]:
    """Notificaciones en BD que aun no tienen archivos descargados."""
    if not DB_PATH.exists():
        return []

    with _connect() as con:
        columns = _get_table_columns(con)
        notif_col = _find_notification_column(columns)
        if not notif_col:
            return []

        q = f'SELECT DISTINCT "{notif_col}" FROM notificaciones WHERE "{notif_col}" IS NOT NULL AND TRIM("{notif_col}") <> ""'
        db_notifs = {str(r[0]).strip() for r in con.execute(q).fetchall() if str(r[0] or "").strip()}

    downloaded_notifs = _notifications_with_files(DOWNLOADS_DIR)
    return sorted(n for n in db_notifs if n not in downloaded_notifs)


def _minutes_since_last_sync() -> int | None:
    """Minutos desde la ultima modificacion de la BD local."""
    try:
        if not DB_PATH.exists():
            return None
        elapsed_seconds = max(0.0, time.time() - DB_PATH.stat().st_mtime)
        return int(elapsed_seconds // 60)
    except OSError:
        return None


def _remote_check_required() -> tuple[bool, int | None]:
    """Indica si conviene forzar una verificacion remota en SNE."""
    minutes = _minutes_since_last_sync()
    if minutes is None:
        return True, None
    return minutes >= REMOTE_CHECK_STALE_MINUTES, minutes


def _head_actions_html() -> str:
    return """
    <div class="head-actions">
      <div class="notif-wrap">
        <button type="button" class="btn bell-btn" onclick="toggleNotificationCenter(event)" aria-label="Notificaciones">
          <span class="bell-icon">&#128276;</span>
          <span id="bellBadge" class="bell-badge" style="display:none;">0</span>
        </button>
        <div id="notificationPanel" class="notif-panel">
          <div class="notif-title">Notificaciones</div>
          <ul id="notificationList" class="notif-list">
            <li class="muted">Sin notificaciones por ahora.</li>
          </ul>
        </div>
      </div>
      <button class="btn stats-btn" onclick="abrirEstadisticas()" title="Ver estadísticas">&#128202; Estadísticas</button>
      <button class="btn refresh" onclick="abrirActualizacion(event)"><span class="spinner"></span>Actualizar</button>
    </div>
    """


def _html_page(title: str, body: str) -> HTMLResponse:
    page = f"""<!doctype html>
<html lang=\"es\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #eff4fb;
      --card: #ffffff;
      --ink: #132236;
      --muted: #607188;
      --brand: #0057b8;
      --brand-2: #0284c7;
      --line: #d3dfec;
      --accent: #eef6ff;
      --success: #1f9d55;
      --shadow: 0 18px 42px rgba(11, 36, 66, 0.13);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Trebuchet MS", "Lucida Sans Unicode", "Lucida Grande", "Lucida Sans", Arial, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at 8% 5%, rgba(2, 132, 199, 0.14), transparent 40%),
        radial-gradient(circle at 95% 0%, rgba(0, 87, 184, 0.18), transparent 35%),
        linear-gradient(180deg, #f5f9ff 0%, var(--bg) 70%);
    }}
    .wrap {{ max-width: 1440px; margin: 0 auto; padding: 24px; }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 20px;
      background: linear-gradient(112deg, #003d88 0%, #0057b8 52%, #0284c7 100%);
      color: #fff;
      padding: 18px 22px;
      flex-wrap: wrap;
    }}
    .head > div:first-child {{ flex: 1; }}
    .head h1 {{ margin: 0; font-size: 24px; font-weight: 700; letter-spacing: 0.2px; }}
    .head-actions {{ display: inline-flex; align-items: center; gap: 10px; }}
    .meta {{ font-size: 13px; opacity: 0.92; margin-top: 6px; }}
    .content {{ padding: 20px; }}
    .toolbar {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; align-items: center; }}
    input[type=text] {{
      min-width: 260px;
      flex: 1;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      font-size: 14px;
      transition: all 0.2s;
    }}
    input[type=text]:focus {{
      outline: none;
      border-color: var(--brand);
      box-shadow: 0 0 0 3px rgba(15, 90, 165, 0.1);
    }}
    button, .btn {{
      border: 0;
      border-radius: 10px;
      background: var(--brand);
      color: #fff;
      padding: 10px 15px;
      text-decoration: none;
      font-size: 14px;
      font-weight: 500;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s ease;
      white-space: nowrap;
    }}
    button:hover, .btn:hover {{
      background: #0d4a8f;
      transform: translateY(-1px);
      box-shadow: 0 4px 12px rgba(15, 90, 165, 0.25);
    }}
    button:active {{ transform: translateY(0); }}
    button:disabled {{
      opacity: 0.5;
      cursor: not-allowed;
      transform: none;
    }}
    .btn.secondary {{ background: #425f7f; }}
    .btn.secondary:hover {{ background: #2f4a67; }}
    .btn.stats-btn {{
      background: linear-gradient(110deg, #6d28d9, #8b5cf6);
      padding: 10px 15px;
      font-weight: 600;
    }}
    .btn.stats-btn:hover {{ background: linear-gradient(110deg, #5b21b6, #7c3aed); }}
    .btn.refresh {{
      background: linear-gradient(110deg, #1f9d55, #3abf72);
      padding: 10px 18px;
      font-weight: 600;
    }}
    .btn.refresh:hover {{ background: linear-gradient(110deg, #188a4a, #2eab63); }}
    #statsModal {{
      position: fixed; inset: 0;
      background: rgba(0,0,0,0.55);
      display: none; align-items: center; justify-content: center;
      z-index: 9999;
    }}
    #statsModal.active {{ display: flex; }}
    .stats-box {{
      background: #fff;
      border-radius: 18px;
      box-shadow: 0 24px 64px rgba(0,0,0,0.22);
      padding: 32px 36px;
      min-width: 360px;
      max-width: 620px;
      width: 100%;
      max-height: 90vh;
      overflow-y: auto;
    }}
    .stats-header {{
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 20px;
    }}
    .stats-header h2 {{ margin: 0; font-size: 20px; color: #132236; }}
    .stats-close {{
      background: none; border: none; font-size: 24px; cursor: pointer;
      color: #607188; line-height: 1; padding: 0 4px;
    }}
    .stats-close:hover {{ color: #132236; }}
    .stats-total {{
      font-size: 13px; color: #607188; margin-bottom: 18px;
    }}
    .stats-bar-row {{
      margin-bottom: 12px;
    }}
    .stats-label {{
      display: flex; justify-content: space-between;
      font-size: 14px; font-weight: 500; margin-bottom: 4px;
      color: #132236;
    }}
    .stats-label .stats-count {{
      font-weight: 700; color: #0057b8;
    }}
    .stats-track {{
      background: #e8f0fb; border-radius: 999px; height: 14px; overflow: hidden;
    }}
    .stats-fill {{
      height: 100%; border-radius: 999px;
      background: linear-gradient(90deg, #0057b8, #0284c7);
      transition: width 0.5s ease;
    }}
    .notif-wrap {{ position: relative; }}
    .bell-btn {{
      position: relative;
      background: rgba(255,255,255,0.16);
      border: 1px solid rgba(255,255,255,0.28);
      padding: 10px 12px;
      min-width: 46px;
      justify-content: center;
    }}
    .bell-btn:hover {{ background: rgba(255,255,255,0.26); }}
    .bell-icon {{ font-size: 18px; line-height: 1; }}
    .bell-badge {{
      position: absolute;
      top: -6px;
      right: -6px;
      min-width: 20px;
      height: 20px;
      border-radius: 999px;
      background: #ff5f6d;
      color: #fff;
      font-size: 12px;
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 0 6px;
      border: 2px solid #ffffff;
    }}
    .notif-panel {{
      position: absolute;
      top: calc(100% + 10px);
      right: 0;
      width: min(420px, 86vw);
      max-height: 340px;
      overflow: auto;
      background: #ffffff;
      color: var(--ink);
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: 0 18px 34px rgba(9, 29, 51, 0.24);
      padding: 10px;
      display: none;
      z-index: 1300;
    }}
    .notif-panel.open {{ display: block; }}
    .notif-title {{ font-weight: 700; font-size: 14px; margin: 2px 2px 8px; color: #0b3f76; }}
    .notif-list {{ list-style: none; padding: 0; margin: 0; }}
    .notif-list li {{
      padding: 8px 10px;
      border-radius: 8px;
      font-size: 13px;
      border: 1px solid #e8eef7;
      margin-bottom: 7px;
      background: #f8fbff;
    }}
    .notif-list li strong {{ color: #0b3f76; }}
    .spinner {{
      display: inline-block;
      width: 14px;
      height: 14px;
      border: 2px solid rgba(255,255,255,0.3);
      border-top-color: #fff;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    .modal {{
      display: none;
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0,0,0,0.5);
      z-index: 1000;
      align-items: center;
      justify-content: center;
      backdrop-filter: blur(4px);
    }}
    .modal.active {{ display: flex; }}
    .modal-content {{
      background: var(--card);
      border-radius: 16px;
      padding: 40px;
      text-align: center;
      box-shadow: 0 20px 60px rgba(0,0,0,0.3);
      max-width: 420px;
      animation: slideUp 0.3s ease;
    }}
    @keyframes slideUp {{
      from {{ opacity: 0; transform: translateY(20px); }}
      to {{ opacity: 1; transform: translateY(0); }}
    }}
    .modal-content h2 {{ margin: 0 0 12px; color: var(--ink); font-size: 20px; }}
    .modal-spinner {{
      width: 52px; height: 52px;
      margin: 0 auto 24px;
      border: 3px solid var(--line);
      border-top-color: var(--brand);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }}
    .modal-text {{ color: var(--muted); font-size: 14px; line-height: 1.5; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 11px 10px; text-align: left; vertical-align: top; }}
    th {{ background: var(--accent); position: sticky; top: 0; font-weight: 700; color: #0b3f76; z-index: 2; }}
    .table-wrap {{ max-height: 72vh; overflow: auto; border: 1px solid var(--line); border-radius: 12px; background: #fff; }}
    tr:hover {{ background: #fafbfc; }}
    .muted {{ color: var(--muted); font-size: 13px; }}
    .badge {{
      background: #eaf3ff;
      color: #0f5aa5;
      border: 1px solid #cde0f7;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 500;
      padding: 3px 9px;
      white-space: nowrap;
      display: inline-block;
    }}
    ul.file-list {{ margin: 12px 0 0; padding-left: 18px; }}
    .file-list li {{ margin: 8px 0; }}
    .file-list a {{ color: var(--brand); text-decoration: none; font-weight: 500; }}
    .file-list a:hover {{ text-decoration: underline; }}
    .info-box {{
      background: #edf5ff;
      border-left: 4px solid var(--brand);
      border-radius: 10px;
      padding: 12px 14px;
      margin-bottom: 12px;
      font-size: 13px;
      color: #0d4a8f;
    }}
    .kpi-row {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 12px; }}
    .kpi {{
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      padding: 10px 12px;
      min-width: 160px;
      box-shadow: 0 5px 16px rgba(11, 36, 66, 0.06);
    }}
    .kpi .k-label {{ font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.4px; }}
    .kpi .k-value {{ font-size: 18px; font-weight: 700; color: #0b3f76; margin-top: 2px; }}
    .pager {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }}
    .pager-group {{ display: inline-flex; gap: 8px; }}
    .page-chip {{
      border: 1px solid #b8d2ef;
      background: #eef6ff;
      color: #1b4f86;
      border-radius: 999px;
      padding: 7px 12px;
      font-size: 12px;
      font-weight: 700;
    }}
    .btn.disabled {{
      pointer-events: none;
      opacity: 0.45;
      box-shadow: none;
      transform: none;
    }}
    .accordion-row {{ display: none; background: #f8fbff; }}
    .accordion-row.open {{ display: table-row; }}
    .accordion-cell {{ padding: 14px 12px !important; }}
    .accordion-box {{
      border: 1px solid #d7e6f7;
      border-radius: 8px;
      background: #fff;
      padding: 10px 12px;
    }}
    .docs-empty {{ color: var(--muted); font-size: 13px; }}
    .docs-list {{ margin: 0; padding-left: 18px; }}
    .docs-list li {{ margin: 6px 0; }}
    .floating-alert {{
      position: fixed;
      right: 18px;
      bottom: 18px;
      z-index: 1200;
      min-width: 280px;
      max-width: 360px;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #edf9f1;
      color: #1d7d46;
      box-shadow: 0 14px 30px rgba(10, 28, 46, 0.16);
      padding: 12px 14px;
      display: none;
    }}
    .floating-alert.show {{ display: block; }}
    .floating-alert.pending {{ background: #fff6e8; color: #9a5b00; border-color: #f2d39a; }}
    .floating-alert.check {{ background: #f6f2ff; color: #4d3b8f; border-color: #d8cbf7; }}
    .fa-close {{
      position: absolute;
      top: 8px;
      right: 8px;
      border: 0;
      background: transparent;
      color: inherit;
      width: 24px;
      height: 24px;
      border-radius: 50%;
      cursor: pointer;
      font-size: 16px;
      line-height: 24px;
      padding: 0;
      opacity: 0.75;
    }}
    .fa-close:hover {{ opacity: 1; background: rgba(0, 0, 0, 0.08); }}
    .fa-title {{ font-weight: 700; margin-bottom: 6px; }}
    .fa-text {{ font-size: 13px; line-height: 1.4; }}
    .fa-actions {{ margin-top: 10px; text-align: right; }}
    .fa-actions .btn {{ padding: 8px 10px; font-size: 13px; }}
  </style>
</head>
<body>
  <div class=\"wrap\">{body}</div>
  <div id=\"updateModal\" class=\"modal\">
    <div class=\"modal-content\">
      <h2>Actualizando notificaciones...</h2>
      <div class=\"modal-spinner\"></div>
      <p class=\"modal-text\" id=\"updateProgress\">Conectando con el servidor...</p>
    </div>
  </div>
  <div id=\"statsModal\">
    <div class=\"stats-box\">
      <div class=\"stats-header\">
        <h2>&#128202; Estadísticas de documentos</h2>
        <button class=\"stats-close\" onclick=\"cerrarEstadisticas()\">&times;</button>
      </div>
      <div class=\"stats-total\" id=\"statsTotal\">Cargando...</div>
      <div id=\"statsContent\"></div>
    </div>
  </div>
  <div id="floatingAlert" class="floating-alert" aria-live="polite">
    <button type="button" class="fa-close" aria-label="Cerrar alerta" onclick="closeFloatingAlert()">&times;</button>
    <div class="fa-title" id="floatingAlertTitle">Estado de descargas</div>
    <div class="fa-text" id="floatingAlertText">Verificando pendientes...</div>
    <div class="fa-actions">
      <button id="floatingUpdateBtn" type="button" class="btn refresh" onclick="abrirActualizacion(event)">Actualizar</button>
    </div>
  </div>
  <script>
    const STATS_COLORS = [
      'linear-gradient(90deg,#0057b8,#0284c7)',
      'linear-gradient(90deg,#6d28d9,#8b5cf6)',
      'linear-gradient(90deg,#1f9d55,#3abf72)',
      'linear-gradient(90deg,#d97706,#fbbf24)',
      'linear-gradient(90deg,#dc2626,#f87171)',
      'linear-gradient(90deg,#0891b2,#22d3ee)',
      'linear-gradient(90deg,#7c3aed,#a78bfa)',
      'linear-gradient(90deg,#059669,#34d399)',
    ];
    async function abrirEstadisticas() {{
      document.getElementById('statsModal').classList.add('active');
      document.getElementById('statsTotal').textContent = 'Cargando...';
      document.getElementById('statsContent').innerHTML = '';
      try {{
        const res = await fetch('/api/estadisticas');
        const data = await res.json();
        const tipos = data.tipos || [];
        const total = data.total || 0;
        document.getElementById('statsTotal').textContent =
          `${{total}} documento${{total !== 1 ? 's' : ''}} en ${{tipos.length}} tipo${{tipos.length !== 1 ? 's' : ''}}`;
        const max = tipos.length > 0 ? tipos[0].cantidad : 1;
        document.getElementById('statsContent').innerHTML = tipos.map((t, i) => {{
          const pct = Math.round((t.cantidad / max) * 100);
          const color = STATS_COLORS[i % STATS_COLORS.length];
          return `<div class="stats-bar-row">
            <div class="stats-label">
              <span>${{t.tipo}}</span>
              <span class="stats-count">${{t.cantidad}}</span>
            </div>
            <div class="stats-track">
              <div class="stats-fill" style="width:${{pct}}%;background:${{color}}"></div>
            </div>
          </div>`;
        }}).join('');
      }} catch(e) {{
        document.getElementById('statsTotal').textContent = 'Error al cargar estadísticas.';
      }}
    }}
    function cerrarEstadisticas() {{
      document.getElementById('statsModal').classList.remove('active');
    }}
    document.getElementById('statsModal').addEventListener('click', function(e) {{
      if (e.target === this) cerrarEstadisticas();
    }});

    const bellState = {{ items: [], keys: new Set(), pendingCount: 0 }};

    function nowLabel() {{
      const d = new Date();
      return d.toLocaleTimeString('es-PE', {{ hour: '2-digit', minute: '2-digit' }});
    }}

    function pushNotification(text, level = 'info') {{
      const clean = (text || '').trim();
      if (!clean) return;
      const key = `${{level}}|${{clean}}`;
      if (bellState.keys.has(key)) return;
      bellState.keys.add(key);
      bellState.items.unshift({{ text: clean, level, time: nowLabel() }});
      if (bellState.items.length > 40) bellState.items = bellState.items.slice(0, 40);
      renderNotificationCenter();
    }}

    function renderNotificationCenter() {{
      const list = document.getElementById('notificationList');
      const badge = document.getElementById('bellBadge');
      if (!list || !badge) return;

      if (!bellState.items.length) {{
        list.innerHTML = '<li class="muted">Sin notificaciones por ahora.</li>';
      }} else {{
        list.innerHTML = bellState.items.map((n) => (
          `<li><strong>${{n.time}}</strong> · ${{n.text}}</li>`
        )).join('');
      }}

      const count = bellState.pendingCount > 0 ? bellState.pendingCount : bellState.items.length;
      if (count > 0) {{
        badge.style.display = 'inline-flex';
        badge.textContent = String(Math.min(count, 99));
      }} else {{
        badge.style.display = 'none';
      }}
    }}

    function toggleNotificationCenter(evt) {{
      if (evt) evt.stopPropagation();
      const panel = document.getElementById('notificationPanel');
      if (!panel) return;
      panel.classList.toggle('open');
    }}

    document.addEventListener('click', (evt) => {{
      const panel = document.getElementById('notificationPanel');
      const wrap = evt.target && evt.target.closest ? evt.target.closest('.notif-wrap') : null;
      if (panel && !wrap) panel.classList.remove('open');
    }});

    let floatingAlertDismissed = false;
    let floatingAlertStateSignature = '';

    function closeFloatingAlert() {{
      floatingAlertDismissed = true;
      const box = document.getElementById('floatingAlert');
      if (box) box.classList.remove('show');
    }}

    async function abrirActualizacion(evt) {{
      const modal = document.getElementById('updateModal');
      const btn = (evt && evt.target && evt.target.closest('button'))
        ? evt.target.closest('button')
        : document.querySelector('.btn.refresh');
      if (btn) btn.disabled = true;
      modal.classList.add('active');
      
      try {{
        const resp = await fetch('/api/actualizar', {{ method: 'POST' }});
        const data = await resp.json();
        
        if(data.success) {{
          const checkInterval = setInterval(async () => {{
            const status = await fetch('/api/estado').then(r => r.json());
            const shownProgress = status.running
              ? Math.min(status.progress || 0, 99)
              : 100;
            document.getElementById('updateProgress').textContent = `Descargando... ${{shownProgress}}%`;
            
            if(!status.running) {{
              clearInterval(checkInterval);
              if (Array.isArray(status.recent_downloads) && status.recent_downloads.length > 0) {{
                status.recent_downloads.forEach((name) => pushNotification(`Se ha descargado: ${{name}}`, 'download'));
              }} else {{
                pushNotification(status.message || 'Actualización finalizada.', 'status');
              }}
              document.getElementById('updateProgress').textContent = 'Descargando... 100%';
              setTimeout(() => {{
                document.getElementById('updateProgress').textContent = status.message || '¡Completado! Recargando...';
              }}, 350);
              setTimeout(() => {{
                modal.classList.remove('active');
                if (btn) btn.disabled = false;
                location.reload();
              }}, 1200);
            }}
          }}, 800);
        }} else {{
          alert('Error: ' + (data.error || 'Desconocido'));
          modal.classList.remove('active');
          if (btn) btn.disabled = false;
        }}
      }} catch(err) {{
        alert('Error de conexión: ' + err);
        modal.classList.remove('active');
        if (btn) btn.disabled = false;
      }}
    }}

    async function refreshFloatingAlert() {{
      const box = document.getElementById('floatingAlert');
      const title = document.getElementById('floatingAlertTitle');
      const text = document.getElementById('floatingAlertText');
      const btn = document.getElementById('floatingUpdateBtn');
      if (!box || !title || !text || !btn) return;

      try {{
        const res = await fetch('/api/pending');
        const data = await res.json();
        const pending = Number(data.pending_count || 0);
        const needsCheck = Boolean(data.needs_remote_check);
        const minutesSinceSync = data.minutes_since_last_sync;
        const nextSignature = `${{pending}}|${{needsCheck ? 1 : 0}}|${{minutesSinceSync ?? 'na'}}`;
        if (nextSignature !== floatingAlertStateSignature) {{
          floatingAlertDismissed = false;
          floatingAlertStateSignature = nextSignature;
        }}

        if (floatingAlertDismissed) {{
          box.classList.remove('show');
          return;
        }}

        bellState.pendingCount = pending > 0 ? pending : (needsCheck ? 1 : 0);
        renderNotificationCenter();
        box.classList.add('show');
        box.classList.remove('check');

        if (pending > 0) {{
          box.classList.add('pending');
          title.textContent = 'Hay novedades';
          text.textContent = `Tienes ${{pending}} notificación(es) pendiente(s) por descargar.`;
          pushNotification(`Hay ${{pending}} notificación(es) pendiente(s) para actualizar.`, 'pending');
          btn.style.display = 'inline-flex';
        }} else if (needsCheck) {{
          box.classList.remove('pending');
          box.classList.add('check');
          title.textContent = 'Revisión recomendada';
          if (typeof minutesSinceSync === 'number') {{
            text.textContent = `La última sincronización fue hace ${{minutesSinceSync}} min. Presiona "Actualizar" para verificar si hay nuevas notificaciones en SNE.`;
          }} else {{
            text.textContent = 'No hay historial local de sincronización. Presiona "Actualizar" para verificar nuevas notificaciones en SNE.';
          }}
          pushNotification('Conviene revisar nuevas notificaciones. Presiona Actualizar.', 'check');
          btn.style.display = 'inline-flex';
        }} else {{
          box.classList.remove('pending');
          title.textContent = 'Todo al día';
          text.textContent = 'No hay pendientes por descargar en este momento.';
          btn.style.display = 'none';
        }}
      }} catch (e) {{
        bellState.pendingCount = 0;
        renderNotificationCenter();
        box.classList.add('show');
        box.classList.remove('pending');
        box.classList.remove('check');
        title.textContent = 'Estado';
        text.textContent = 'No se pudo verificar pendientes ahora.';
        btn.style.display = 'none';
      }}
    }}

    renderNotificationCenter();
    refreshFloatingAlert();
    setInterval(refreshFloatingAlert, 20000);

    async function toggleDocs(btn, numero, rowId) {{
      const row = document.getElementById(`docs-row-${{rowId}}`);
      const body = document.getElementById(`docs-body-${{rowId}}`);
      if (!row || !body) return;

      if (row.classList.contains('open')) {{
        row.classList.remove('open');
        btn.textContent = 'Ver documentos';
        return;
      }}

      row.classList.add('open');
      btn.textContent = 'Ocultar documentos';

      if (body.dataset.loaded === '1') return;
      body.innerHTML = '<div class="docs-empty">Cargando documentos...</div>';

      try {{
        const resp = await fetch(`/api/notificaciones/${{encodeURIComponent(numero)}}/documentos`);
        const data = await resp.json();
        if (!data || !Array.isArray(data.files) || data.files.length === 0) {{
          body.innerHTML = '<div class="docs-empty">No hay documentos para esta notificación.</div>';
          body.dataset.loaded = '1';
          return;
        }}

        const items = data.files.map((f) => (
          `<li><a href="${{f.href}}" target="_blank"><strong>${{f.name}}</strong></a> ` +
          `<span class="muted">(${{f.date_folder}} | ${{f.size_kb}} KB | Vence: ${{f.due_date || '-'}} | Tipo: ${{f.document_type || 'No identificado'}})</span></li>`
        )).join('');
        body.innerHTML = `<ul class="docs-list">${{items}}</ul>`;
        body.dataset.loaded = '1';
      }} catch (err) {{
        body.innerHTML = '<div class="docs-empty">No se pudo cargar el detalle de documentos.</div>';
      }}
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(content=page)


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "db": str(DB_PATH),
        "downloads": str(DOWNLOADS_DIR),
    }


@app.post("/api/actualizar")
def actualizar():
    """Inicia la actualización en background."""
    if UPDATE_STATE["running"]:
        return JSONResponse({"success": False, "error": "Ya se está ejecutando una actualización"}, status_code=400)

    UPDATE_STATE["progress"] = 5
    UPDATE_STATE["started_at"] = time.time()
    thread = threading.Thread(target=_run_update, daemon=True)
    thread.start()
    return JSONResponse({"success": True, "message": "Actualización iniciada"})


@app.get("/api/estado")
def estado():
    """Devuelve el estado actual de la actualización."""
    progress = int(UPDATE_STATE.get("progress") or 0)
    if UPDATE_STATE["running"]:
        started_at = UPDATE_STATE.get("started_at")
        if started_at:
            elapsed = max(0.0, time.time() - float(started_at))
            estimated = min(95, 10 + int(elapsed / 1.2))
            progress = max(progress, estimated)
        progress = min(progress, 99)
    else:
        if UPDATE_STATE.get("error"):
            progress = max(progress, 0)
        else:
            progress = 100

    return JSONResponse({
        "running": UPDATE_STATE["running"],
        "progress": progress,
        "error": UPDATE_STATE["error"],
        "message": UPDATE_STATE["message"],
        "recent_downloads": UPDATE_STATE.get("recent_downloads", []),
    })


@app.get("/api/pending")
def pending_status():
    pending = _get_pending_notifications()
    needs_remote_check, minutes_since_last_sync = _remote_check_required()
    return JSONResponse({
        "pending_count": len(pending),
        "pending_notifications": pending,
        "needs_remote_check": needs_remote_check,
        "minutes_since_last_sync": minutes_since_last_sync,
        "stale_after_minutes": REMOTE_CHECK_STALE_MINUTES,
    })


@app.get("/", response_class=HTMLResponse)
def index(
  q: str = Query(default="", description="Texto para buscar"),
  page: int = Query(default=1, ge=1),
) -> HTMLResponse:
    head_actions = _head_actions_html()

    if not DB_PATH.exists():
        return _html_page(
            "OsiDOc Viewer",
            f"""
<div class="card">
  <div class="head">
    <div><h1>OsiDOc Viewer</h1><div class="meta">Base de datos no encontrada</div></div>
    {head_actions}
  </div>
  <div class="content">
    <div class="info-box">No existe la base de datos SQLite. Primero ejecuta <code>osinergmin_auth.py</code> para generar la data.</div>
  </div>
</div>
""",
        )

    with _connect() as con:
        columns = _get_table_columns(con)
        if not columns:
            return _html_page(
                "OsiDOc Viewer",
                f"""
<div class="card">
  <div class="head"><div><h1>OsiDOc Viewer</h1><span class="badge">Sin tabla</span></div>{head_actions}</div>
  <div class="content">No se encontró la tabla notificaciones.</div>
</div>
""",
            )

        where_clause = ""
        params: list[str | int] = []
        if q.strip():
            where_parts = [f'CAST("{c}" AS TEXT) LIKE ?' for c in columns]
            where_clause = " WHERE " + " OR ".join(where_parts)
            params.extend([f"%{q.strip()}%"] * len(columns))

        total = int(con.execute(f"SELECT COUNT(*) FROM notificaciones{where_clause}", params).fetchone()[0])
        page_size = PAGE_SIZE_FIXED
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        query = f"SELECT rowid, * FROM notificaciones{where_clause} ORDER BY rowid DESC LIMIT ? OFFSET ?"
        rows = con.execute(query, params + [page_size, offset]).fetchall()

    notif_col = _find_notification_column(columns)
    due_col = _find_due_date_column(columns)
    asunto_col = next((c for c in columns if c.lower() == "asunto"), None)
    notif_date_col = next((c for c in columns if c.lower() in ["fecha_de_notificacion", "fecha_notificacion"]), None)

    pending_notifs = _get_pending_notifications()
    pending_count = len(pending_notifs)
    needs_remote_check, minutes_since_last_sync = _remote_check_required()

    display_cols = [c for c in columns if c.lower() in [dc.lower() for dc in COLUMNS_TO_DISPLAY]]
    head = "".join(f"<th>{html.escape(c)}</th>" for c in display_cols)
    head += "<th>Fecha de vencimiento</th><th>Tipo de documento</th><th>Documentos</th>"
    body_rows: list[str] = []
    for row in rows:
        tds = []
        for c in display_cols:
            val = str(row[c] if row[c] is not None else "")
            tds.append(f"<td>{html.escape(val[:50])}</td>" if len(val) > 50 else f"<td>{html.escape(val)}</td>")

        source_text = ""
        if asunto_col and row[asunto_col] is not None:
          source_text = str(row[asunto_col])
        elif notif_col and row[notif_col] is not None:
          source_text = str(row[notif_col])

        notif = str(row[notif_col]) if notif_col and row[notif_col] else ""
        due_value = ""
        doc_type = _infer_document_type(source_text)
        notif_date_text = str(row[notif_date_col]) if notif_date_col and row[notif_date_col] is not None else ""
        if notif:
          due_from_docs, doc_type_from_docs = _summarize_notification_metadata(notif, source_text, notif_date_text)
          if due_from_docs:
            due_value = due_from_docs
          if doc_type_from_docs:
            doc_type = doc_type_from_docs

        if not due_value and due_col and row[due_col] is not None:
          due_value = str(row[due_col]).strip()
        due_display = due_value if due_value else ("Sin fecha explicita" if notif else "-")
        tds.append(f"<td>{html.escape(due_display)}</td>")
        tds.append(f"<td>{html.escape(doc_type)}</td>")

        row_id = int(row["rowid"])
        if notif:
            docs_link = (
                f"<button type='button' class='btn secondary' "
                f"onclick=\"toggleDocs(this, '{html.escape(notif)}', {row_id})\">Ver documentos</button>"
            )
        else:
            docs_link = "<span class='muted'>Sin Nro.</span>"
        tds.append(f"<td>{docs_link}</td>")

        body_rows.append("<tr>" + "".join(tds) + "</tr>")
        body_rows.append(
            "<tr id='docs-row-{}' class='accordion-row'><td colspan='{}' class='accordion-cell'>"
            "<div id='docs-body-{}' class='accordion-box' data-loaded='0'></div>"
            "</td></tr>".format(row_id, len(display_cols) + 1, row_id)
        )

    prev_page = max(1, page - 1)
    total_pages = max(1, (total + page_size - 1) // page_size)
    next_page = min(total_pages, page + 1)
    start_row = (page - 1) * page_size + 1 if total > 0 else 0
    end_row = min(page * page_size, total)

    body = f"""
<div class="card">
  <div class="head">
    <div>
      <h1>OsiDOc Viewer</h1>
      <div class="meta">📊 {total} registros en BD</div>
    </div>
    {head_actions}
  </div>
  <div class="content">
    <form class="toolbar" method="get" action="/">
      <input type="text" name="q" value="{html.escape(q)}" placeholder="🔍 Buscar en cualquier columna..." />
      <button type="submit">Buscar</button>
      <a class="btn secondary" href="/">Limpiar</a>
    </form>
    <div class="kpi-row">
      <div class="kpi"><div class="k-label">Total Registros</div><div class="k-value">{total}</div></div>
      <div class="kpi"><div class="k-label">Página Actual</div><div class="k-value">{page}/{total_pages}</div></div>
      <div class="kpi"><div class="k-label">Bloque</div><div class="k-value">{start_row}-{end_row}</div></div>
      <div class="kpi"><div class="k-label">Tamaño Página</div><div class="k-value">10</div></div>
    </div>
    <div class="info-box">
      📄 Mostrando {start_row} a {end_row} de {total} registros (siempre 10 por página).
    </div>
    <div class="info-box" style="border-left-color: {'#e67e22' if (pending_count > 0 or needs_remote_check) else '#27ae60'}; background: {'#fff6e8' if (pending_count > 0 or needs_remote_check) else '#edf9f1'}; color: {'#9a5b00' if (pending_count > 0 or needs_remote_check) else '#1d7d46'};">
      {'⚠️ Hay ' + str(pending_count) + ' notificación(es) pendiente(s) por descargar. Presiona "Actualizar".' if pending_count > 0 else ('⚠️ La última sincronización local está desactualizada' + (' (hace ' + str(minutes_since_last_sync) + ' min)' if minutes_since_last_sync is not None else '') + '. Presiona "Actualizar" para verificar nuevas notificaciones en SNE.' if needs_remote_check else '✅ No hay pendientes por descargar en este momento.')}
    </div>
    <div class="pager">
      <div class="pager-group">
        <a class="btn secondary {'disabled' if page <= 1 else ''}" href="/?q={quote(q)}&page={prev_page}">← Anterior</a>
        <a class="btn secondary {'disabled' if page >= total_pages else ''}" href="/?q={quote(q)}&page={next_page}">Siguiente →</a>
      </div>
      <span class="page-chip">Página {page} de {total_pages}</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>{head}</tr></thead>
        <tbody>{''.join(body_rows) if body_rows else '<tr><td colspan="999" style="text-align:center; color:#999;">Sin resultados</td></tr>'}</tbody>
      </table>
    </div>
  </div>
</div>
"""
    return _html_page("OsiDOc Viewer", body)


@app.get("/notificaciones/{numero}/documentos", response_class=HTMLResponse)
def documentos(numero: str) -> HTMLResponse:
    docs = _build_notification_files_metadata(numero)

    if not docs:
        body = f"""
<div class="card">
  <div class="head"><h1>Documentos de {html.escape(numero)}</h1><span class="badge">0 archivos</span></div>
  <div class="content">
    <p>No se encontraron archivos para este número.</p>
    <a class="btn secondary" href="/">Volver al listado</a>
  </div>
</div>
"""
        return _html_page(f"Documentos {numero}", body)

    items: list[str] = []
    for doc in docs:
        due_display = doc['due_date'] or (
          f"{doc['deadline_days']} dias (estimado por plazo)" if doc.get('deadline_days') else "Sin fecha explicita"
        )
        items.append(
            f"<li><a href='{html.escape(doc['href'])}' target='_blank'><strong>{html.escape(doc['name'])}</strong></a> "
            f"<span class='muted'>({html.escape(doc['date_folder'])} | {html.escape(doc['size_kb'])} KB | "
            f"Vence: {html.escape(due_display)} | Tipo: {html.escape(doc['document_type'])})</span></li>"
        )

    body = f"""
<div class="card">
  <div class="head"><h1>📄 Documentos de {html.escape(numero)}</h1><span class="badge">{len(docs)} archivos</span></div>
  <div class="content">
    <a class="btn secondary" href="/">← Volver al listado</a>
    <ul class="file-list">{''.join(items)}</ul>
  </div>
</div>
"""
    return _html_page(f"Documentos {numero}", body)


@app.get("/api/estadisticas")
def estadisticas():
    """Devuelve conteo de documentos por tipo de documento."""
    if not DB_PATH.exists():
        return JSONResponse({"tipos": [], "total": 0})

    with _connect() as con:
        columns = _get_table_columns(con)
        notif_col = _find_notification_column(columns)
        if not notif_col:
            return JSONResponse({"tipos": [], "total": 0})
        rows = con.execute(
            f"SELECT DISTINCT {notif_col}, asunto FROM notificaciones "
            f"WHERE {notif_col} IS NOT NULL AND TRIM({notif_col}) <> ''"
        ).fetchall()

    counts: dict[str, int] = {}
    total_docs = 0
    for row in rows:
        numero = str(row[0] or "").strip()
        asunto = str(row[1] or "").strip()
        if not numero:
            continue
        files = _build_notification_files_metadata(numero)
        if files:
            for f in files:
                total_docs += 1
                t = f.get("document_type") or _infer_document_type(asunto)
                if not t or t == "No identificado":
                    t = "Otro"
                counts[t] = counts.get(t, 0) + 1
        else:
            t = _infer_document_type(asunto) or "Otro"
            if t == "No identificado":
                t = "Otro"
            counts[t] = counts.get(t, 0) + 1
            total_docs += 1

    sorted_tipos = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return JSONResponse({
        "tipos": [{"tipo": k, "cantidad": v} for k, v in sorted_tipos],
        "total": total_docs,
    })


@app.get("/api/notificaciones/{numero}/documentos")
def documentos_api(numero: str):
    files = _build_notification_files_metadata(numero)

    return JSONResponse({"numero": numero, "files": files})


@app.get("/files/{file_path:path}")
def serve_file(file_path: str):
    candidate = (DOWNLOADS_DIR / file_path).resolve()

    if DOWNLOADS_DIR not in candidate.parents and candidate != DOWNLOADS_DIR:
        raise HTTPException(status_code=400, detail="Ruta invalida")

    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")

    return FileResponse(
        str(candidate),
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=\"{quote(candidate.name)}\""}
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_app:app", host="127.0.0.1", port=8000, reload=False)
