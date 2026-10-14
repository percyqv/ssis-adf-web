# CLAUDE.md — ssis-adf-web (asistente web SSIS → ADF)

Contexto para retomar el trabajo en una sesión nueva (Claude, Copilot o una persona).

## Qué es

Web en **Python (FastAPI)** desplegada en **Azure App Service** para migrar paquetes SSIS a
Azure Data Factory. Combina:

- **Acciones directas sin IA**: botones *Analizar* y *Convertir* que llaman al servidor MCP.
- **Chat con agente**: el modelo **gpt-5.4-mini** (Azure AI Foundry) decide qué herramientas usar
  (function calling). El bucle del agente vive en esta web.
- **Diagrama interactivo** del pipeline ADF (Cytoscape.js + ELK, estilo ADF Studio).
- **Prompts editables y versionados** desde la web, con *Restaurar de fábrica*.

```
Navegador (Easy Auth, Entra ID)
   └─► Web App ssis-adf-web-lab01 (App Service)
         ├─► Azure OpenAI gpt-5.4-mini (FoundryLabInetum)   ← "cerebro"
         ├─► MCP ssis-adf-agent (Container Apps)             ← "manos": analizar/convertir/validar
         ├─► Storage stsiss2adflab01 (listar/subir/descargar, leer pipelines para el diagrama)
         └─► SQLite /home/data/ssis-adf-web/prompts.db      ← versiones de los prompts
```

El servidor MCP está en otro repo: `percyqv/ssis_adf_agent` (rama `feat/azure-blob-http`).

## Estructura

| Archivo | Qué es |
|---|---|
| `app/main.py` | API FastAPI: chat, acciones directas, archivos, diagrama, prompts |
| `app/agent.py` | Bucle del agente (modelo ↔ herramientas MCP + herramientas locales) |
| `app/prompts.py` | Prompt de fábrica (`FACTORY_BASE`), `render()`, `PromptStore` (SQLite) |
| `app/tools.py` | Herramientas locales: `list_artifacts`, `read_artifact`, `get_pipeline_diagram` |
| `app/graph.py` | Pipeline ADF (JSON) → nodos/aristas para el diagrama |
| `app/llm.py` | Cliente Azure OpenAI (API v1, identidad administrada) |
| `app/mcp_client.py` | Cliente MCP (Streamable HTTP + `X-API-Key`) |
| `app/storage.py` | Blob Storage (async) restringido a `ALLOWED_CONTAINERS` |
| `app/config.py` | Configuración desde variables de entorno |
| `app/static/` | UI sin frameworks: `app.js`, `diagram.js`, `prompts.js`, `styles.css`, `vendor/` |
| `scripts/deploy_webapp.ps1` | Despliegue en App Service |
| `tests/` | pytest con modelo, MCP y Storage falsos (`test_app`, `test_actions`, `test_graph`, `test_prompts`) |

## Comandos

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest -q
ruff check .                                           # line-length 100
uvicorn app.main:app --reload                          # local; variables en .env (ver .env.example)
```

Despliegue (PowerShell, `az login` hecho):

```powershell
.\scripts\deploy_webapp.ps1 -OpenAIResource FoundryLabInetum -OpenAIDeployment gpt-5.4-mini `
  -PlanName ASP-IAData01Lab-a9a3 -PlanResourceGroup IA-Data-01-Lab -AdminUsers "<correo>"
```

El primer arranque en B1 puede pasar de 10 min; el script sigue esperando en `/healthz`.

## Recursos en Azure (laboratorio)

| Recurso | Nombre |
|---|---|
| Suscripción | `Inetum-Lab` |
| Web App | `ssis-adf-web-lab01` — https://ssis-adf-web-lab01.azurewebsites.net |
| App Service Plan | `ASP-IAData01Lab-a9a3` (Linux, **B1**, Canada Central), grupo `IA-Data-01-Lab` |
| Modelo | `gpt-5.4-mini` en `FoundryLabInetum` (grupo `FoundryLab`), endpoint `https://foundrylabinetum.openai.azure.com/` |
| MCP | Container App `ssis-adf-agent` (grupo `Siss2adfLab`) |
| Storage | `stsiss2adflab01` (`ssis-packages`, `adf-artifacts`) |
| Autenticación | Easy Auth con Microsoft Entra ID |

La identidad administrada de la web tiene *Storage Blob Data Contributor* y
*Cognitive Services OpenAI User*. La API key del MCP se copia del Container App a la
configuración de la web durante el despliegue; **nunca** se imprime ni se sube al repo.

## Decisiones

- **Híbrido**: lo repetitivo (analizar, convertir, validar) va por botones sin IA, sin coste de
  tokens; el chat sirve para explicar, comparar y dibujar flujos.
- **Seguridad en código, no en el prompt**: solo rutas `az://` de los contenedores permitidos;
  `deploy_to_adf` nunca se ofrece al modelo.
- **Prompts**: *base* (debe contener `{{ENTRADA}}` y `{{SALIDA}}`) + *instrucciones adicionales*.
  Cada guardado es una versión nueva; el de fábrica está en `app/prompts.py` y no se pierde.
  Se guardan en **SQLite** en `/home/data` (persiste entre reinicios y despliegues). Por eso
  gunicorn corre con **1 worker** y SQLite en modo `journal_mode=DELETE` (sin WAL: `/home`
  es una carpeta de red). Si se borra la Web App, la base se pierde con ella.
- **Diagramas**: modelo determinista en `graph.py`; el LLM solo recibe una descripción en texto
  (no dibuja Mermaid/ASCII). ELK necesita el mismo `elk.padding`/espaciado en cada contenedor
  (`nodeLayoutOptions`), si no los contenedores se solapan.
- `ADMIN_USERS` (correos) limita quién edita los prompts; vacío = cualquier usuario con sesión.

## Problemas ya resueltos

- Cuota de App Service B1 = 0 en eastus → se reutilizó el plan existente (pasado a B1).
- El script detecta planes Linux por `kind` o `properties.reserved`.
- Zip deploy: `--async` y tolerar 504 / "failed to start within 10 mins" si el build fue bien.
- `$IsLinux` es variable reservada en PowerShell 7 → `$planIsLinux`.
- El cliente async de Storage necesita `aiohttp`.

## Convenciones

- UI y textos en español, frases simples; sin mayúsculas sostenidas en etiquetas.
- Sin frameworks de frontend; librerías del diagrama vendorizadas en `app/static/vendor/`.
- Commits en inglés. Laboratorio de bajo coste: SKUs mínimos.
