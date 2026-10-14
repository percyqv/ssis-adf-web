# SSIS → ADF · Asistente web

Web con chat de IA para migrar paquetes **SSIS** a **Azure Data Factory**. Usa el servidor MCP
[`ssis-adf-agent`](https://github.com/percyqv/ssis_adf_agent) desplegado en Azure Container Apps.

```
Navegador ──▶ Web App (App Service, Python/FastAPI) ──▶ Azure OpenAI (elige herramientas)
  · login Entra ID          │                              │
  · chat + rutas            │                              ▼
  · subir / descargar       │                 MCP ssis-adf-agent (Container Apps)
                            ▼                              │
                     Storage Account ◀─────────────────────┘
                     ├── ssis-packages/   (.dtsx)
                     └── adf-artifacts/   (JSON de ADF)
```

## Qué hace

- **Chat**: pides en lenguaje natural ("convierte CargaVentas y valida") y el modelo usa las
  herramientas del MCP: `scan_ssis_packages`, `analyze_ssis_package`, `convert_ssis_package`,
  `validate_adf_artifacts`. `deploy_to_adf` no se ofrece al modelo.
- **Rutas de la sesión**: carpeta de entrada y de salida (`az://contenedor/carpeta`), guardadas
  en el navegador y enviadas en cada petición.
- **Paquetes**: lista los `.dtsx` de la entrada y permite subir nuevos. Los botones **Analizar**
  y **Convertir** llaman al MCP directamente (sin IA, sin coste de tokens) y muestran el
  resultado en una tarjeta: complejidad, artefactos generados, validación y avisos.
- **Artefactos**: lista los JSON generados por paquete; se pueden descargar y, en los pipelines,
  abrir el diagrama con **Ver flujo**.
- **Diagrama del pipeline**: visor interactivo al estilo de ADF Studio (Cytoscape.js + ELK):
  actividades con su tipo e icono, dependencias coloreadas por condición, contenedores
  ForEach/Until/If/Switch anidados, panel de detalles (SQL, linked service, reintentos, JSON),
  zoom y descarga en PNG. El chat también lo muestra cuando pides "muéstrame el flujo de …".

- **Prompts del agente** (botón arriba a la derecha): el *prompt base* y las *instrucciones
  adicionales* se editan desde la web. Cada vez que guardas se crea una versión nueva; desde el
  historial puedes volver a cualquiera. El prompt base tiene siempre **Restaurar de fábrica**
  (el texto incluido en el código, `app/prompts.py`). Las versiones se guardan en el Storage,
  en el contenedor `agent-config`. La pestaña *Prompt final* muestra lo que recibe el modelo.

**Seguridad:**
- La API key del MCP y el acceso a Azure OpenAI y al Storage viven solo en el servidor.
- El agente solo acepta rutas `az://` de los contenedores permitidos (`ALLOWED_CONTAINERS`).
- El inicio de sesión lo hace App Service (*Easy Auth*) con Microsoft Entra ID.

## Estructura

| Archivo | Qué es |
|---|---|
| `app/main.py` | API (FastAPI) y página |
| `app/agent.py` | Bucle del agente: modelo ↔ herramientas MCP |
| `app/llm.py` | Cliente de Azure OpenAI (API key o identidad administrada) |
| `app/mcp_client.py` | Cliente MCP (Streamable HTTP + `X-API-Key`) |
| `app/storage.py` | Subir, listar y descargar en Blob Storage |
| `app/prompts.py` | Prompts editables: versión de fábrica, historial y activación |
| `app/graph.py` | Pipeline ADF (JSON) → nodos y aristas para el diagrama |
| `app/tools.py` | Herramientas propias de la web para el agente (artefactos y diagrama) |
| `app/static/diagram.js` | Visor del diagrama; librerías en `app/static/vendor/` |
| `app/static/` | Interfaz (HTML, CSS y JavaScript sin frameworks) |
| `scripts/deploy_webapp.ps1` | Despliegue en App Service |

## Desplegar en Azure (App Service)

Requisitos: Azure CLI con `az login`, el MCP ya desplegado (`ssis-adf-agent`) y un recurso de
**Azure OpenAI / Foundry** con un modelo desplegado que admita *function calling*
con la etiqueta *Finalización de chat* (por ejemplo `gpt-5.4-mini` o `gpt-4.1-mini`).

1. Averigua el nombre del recurso y del despliegue del modelo:
   ```powershell
   az cognitiveservices account list --query "[].{nombre:name, grupo:resourceGroup, tipo:kind}" -o table
   az cognitiveservices account deployment list -n <recurso> -g <grupo> --query "[].{nombre:name, modelo:properties.model.name}" -o table
   ```
2. Desde la carpeta del repositorio:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\scripts\deploy_webapp.ps1 -OpenAIResource <recurso> -OpenAIDeployment <despliegue>
   ```
   Crea el plan (B1, Linux) y la Web App con identidad administrada. Lee la URL y la API key
   del MCP desde el Container App, asigna los permisos (Storage y OpenAI) y publica el código.
   Para publicar cambios, vuelve a ejecutarlo.
3. **Activa el inicio de sesión** (2 minutos, en el portal):
   *Web App → Authentication → Add identity provider → Microsoft →* crear registro de
   aplicación nuevo, *Restrict access: Require authentication* → **Add**.
   Hasta hacerlo, la web es pública.

**Sin coste adicional:** si ya tienes un plan **Linux** de App Service, puedes desplegar la web
en él (en vez de crear otro):
```powershell
.\scripts\deploy_webapp.ps1 -OpenAIResource <recurso> -OpenAIDeployment <despliegue> `
    -PlanName <plan-existente> -PlanResourceGroup <grupo-del-plan>
```
La web se crea en el grupo y la región del plan.

Valores por defecto del script: suscripción `Inetum-Lab`, grupo `Siss2adfLab`, región `eastus`,
Storage `stsiss2adflab01`, web `ssis-adf-web-lab01` (debe ser único en Azure; cámbialo con
`-WebAppName` si está ocupado).

## Ejecutar en local

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
copy .env.example .env      # y completa los valores
# carga las variables de .env en la sesión y arranca:
Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
  $k, $v = $_ -split '=', 2; Set-Item "env:$($k.Trim())" $v.Trim() }
.venv\Scripts\python -m uvicorn app.main:app --reload
```
Abre http://127.0.0.1:8000. En local usa tu `az login` para Azure OpenAI y Storage (necesitas
los roles *Cognitive Services OpenAI User* y *Storage Blob Data Contributor*).

## Configuración (variables de entorno)

| Variable | Descripción |
|---|---|
| `MCP_URL`, `MCP_API_KEY` | Servidor MCP y su API key |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT` | Modelo de IA |
| `AZURE_OPENAI_API_KEY` | Opcional; vacío = identidad administrada / `az login` |
| `AZURE_OPENAI_TOKEN_SCOPE` | Opcional; por defecto según el endpoint (`ai.azure.com` para `*.services.ai.azure.com`, si no `cognitiveservices.azure.com`) |
| `AZURE_OPENAI_API_VERSION` | Por defecto `v1` (API v1 de Azure OpenAI); o una fecha, p. ej. `2024-10-21` |
| `AZURE_STORAGE_ACCOUNT` | Storage de paquetes y artefactos |
| `ALLOWED_CONTAINERS` | Por defecto `ssis-packages,adf-artifacts` |
| `DEFAULT_INPUT`, `DEFAULT_OUTPUT` | Rutas iniciales del panel |
| `ALLOWED_TOOLS` | Herramientas que puede usar el modelo |
| `MAX_AGENT_STEPS`, `MAX_UPLOAD_MB` | Límites (8 pasos, 20 MB) |
| `ADMIN_USERS` | Correos que pueden editar los prompts, separados por coma. Vacío = cualquier usuario con sesión |
| `PROMPTS_CONTAINER` | Contenedor donde se guardan las versiones de los prompts (por defecto `agent-config`; se crea solo) |

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```
Los tests usan un modelo simulado y no necesitan Azure.
