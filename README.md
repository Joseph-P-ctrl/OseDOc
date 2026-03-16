# Autenticacion OsiVirtual

Script base para autenticarse primero contra:
https://osivirtual.osinergmin.gob.pe/autenticacion/acceso-sistema

## Que hace

- Usa sesion HTTP persistente con requests.Session().
- Intenta login por requests (si existe formulario tradicional).
- Si la pagina carga por JavaScript (SPA), hace fallback a Selenium.
- Valida si el login fue exitoso y deja la sesion autenticada para siguientes pasos.

## Instalacion

```bash
pip install -r requirements.txt
```

## Configuracion por variables de entorno

Configura todo por entorno (recomendado). Variables soportadas:

- OSI_USERNAME
- OSI_PASSWORD
- OSI_LOGIN_URL
- OSI_BASE_URL
- OSI_USERNAME_ID
- OSI_PASSWORD_ID
- OSI_LOGIN_FORM_SELECTOR
- OSI_LOGIN_SUBMIT_SELECTOR
- OSI_SUCCESS_URL_KEYWORD
- OSI_HEADLESS
- OSI_TIMEOUT
- OSI_USER_AGENT

Puedes usar el archivo [.env.example](.env.example) como plantilla.

Ejemplo en PowerShell:

```powershell
$env:OSI_USERNAME="tu_usuario"
$env:OSI_PASSWORD="tu_contrasena"
$env:OSI_USERNAME_ID="documentoIdentidad"
$env:OSI_PASSWORD_ID="contrasena"
```

## Ejecucion

```bash
python osinergmin_auth.py
```

Tambien puedes sobreescribir por CLI si lo necesitas.

### Entrada simplificada (main)

Ahora `main.py` solo actua como wrapper y delega la logica al nuevo archivo `app_entry.py`.

Comandos disponibles:

```bash
python main.py sync
python main.py web --host 127.0.0.1 --port 8010
```

## Visualizacion Web (FastAPI)

Se agrego un servicio web para visualizar el listado guardado en SQLite y abrir los documentos descargados.

### 1. Generar data (Excel + SQLite + documentos)

```bash
python osinergmin_auth.py
```

Esto genera/actualiza:

- `downloads/notificaciones.db`
- `downloads/YYYY-MM-DD/<Nro. Notificacion>/...`

### 2. Levantar el servicio web

```bash
python -m uvicorn web_app:app --host 127.0.0.1 --port 8000
```

### 3. Abrir en navegador

- `http://127.0.0.1:8000/` listado completo
- `http://127.0.0.1:8000/health` estado del servicio

### Variables opcionales

- `OSI_SQLITE_PATH` ruta del archivo SQLite (default: `downloads/notificaciones.db`)
- `OSI_DOWNLOAD_DIR` carpeta base de descargas (default: `downloads`)
- `OSI_WEB_PAGE_SIZE` tamaño de pagina para el listado (default: `50`)

## Siguiente paso

Con esta base de autenticacion lista, se puede acoplar la extraccion de documentos filtrados y descargas.
