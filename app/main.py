"""
SSIS → ADF web assistant.

Run locally:   uvicorn app.main:app --reload
App Service:   gunicorn -k uvicorn.workers.UvicornWorker -w 1 -b 0.0.0.0:8000 app.main:app
"""
from __future__ import annotations

import base64
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Agent, ChatModel, SessionSettings
from .config import Settings
from .mcp_client import McpClient, McpError
from .prompts import (
    FACTORY_ID,
    KINDS,
    PLACEHOLDERS,
    PromptError,
    PromptStore,
    default_db_path,
    render,
)
from .storage import Storage, StorageError, parse_location
from .tools import ToolError, build_local_tools, pipeline_graph

log = logging.getLogger("ssis_adf_web")
STATIC = Path(__file__).parent / "static"


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[Message] = Field(min_length=1, max_length=40)
    input_location: str
    output_location: str


class PackageAction(BaseModel):
    package_path: str
    output_location: str | None = None


class ArtifactsAction(BaseModel):
    artifacts_dir: str


class PromptSave(BaseModel):
    text: str
    note: str = ""


class PromptActivate(BaseModel):
    version: str


def create_app(
    settings: Settings | None = None,
    model: ChatModel | None = None,
    mcp: McpClient | None = None,
    storage: Storage | None = None,
    prompt_store: PromptStore | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.mcp = mcp or (
            McpClient(settings.mcp_url, settings.mcp_api_key) if settings.mcp_url else None
        )
        if model is not None:
            app.state.model = model
        elif settings.openai_endpoint and settings.openai_deployment:
            from .llm import AzureOpenAIModel

            app.state.model = AzureOpenAIModel(settings)
        else:
            app.state.model = None
        app.state.storage = storage or (
            Storage(settings.storage_account, settings.allowed_containers)
            if settings.storage_account else None
        )
        app.state.prompts = prompt_store or PromptStore(settings.prompts_db or default_db_path())
        log.info("Prompts en %s", app.state.prompts.path)
        if settings.missing():
            log.warning("Configuración incompleta: %s", ", ".join(settings.missing()))
        yield
        if prompt_store is None:
            app.state.prompts.close()
        if app.state.storage is not None:
            await app.state.storage.close()

    app = FastAPI(title="SSIS → ADF", lifespan=lifespan, docs_url=None, redoc_url=None)

    def _require(component: Any, name: str) -> Any:
        if component is None:
            raise HTTPException(503, f"El servidor no tiene configurado: {name}")
        return component

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "missing_settings": settings.missing()}

    @app.get("/api/config")
    async def config(request: Request) -> dict[str, Any]:
        return {
            "user": _current_user(request),
            "default_input": settings.default_input,
            "default_output": settings.default_output,
            "allowed_containers": settings.allowed_containers,
            "missing_settings": settings.missing(),
        }

    @app.post("/api/chat")
    async def chat(req: ChatRequest, request: Request) -> dict[str, Any]:
        session = SessionSettings(req.input_location.strip(), req.output_location.strip())
        try:
            session.validate(settings.allowed_containers)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            prompt = await request.app.state.prompts.active_texts()
        except Exception:  # never block the chat because of the prompt store
            log.exception("No se pudieron leer los prompts; se usa el de fábrica")
            prompt = None
        agent = Agent(
            prompt=prompt,
            model=_require(request.app.state.model, "AZURE_OPENAI_ENDPOINT / DEPLOYMENT"),
            mcp=_require(request.app.state.mcp, "MCP_URL / MCP_API_KEY"),
            allowed_tools=settings.allowed_tools,
            max_steps=settings.max_agent_steps,
            allowed_containers=settings.allowed_containers,
            local_tools=(
                build_local_tools(request.app.state.storage, session)
                if request.app.state.storage is not None else None
            ),
        )
        try:
            result = await agent.run([m.model_dump() for m in req.messages], session)
        except McpError as exc:
            raise HTTPException(502, f"Error del servidor MCP: {exc}") from exc
        except Exception as exc:  # LLM / network errors
            log.exception("Fallo en el agente")
            raise HTTPException(502, f"No se pudo completar la petición: {exc}") from exc
        log.info("chat user=%s steps=%s", _current_user(request), [s.tool for s in result.steps])
        return {
            "reply": result.reply,
            "steps": [s.__dict__ for s in result.steps],
            "attachments": result.attachments,
        }

    # ---- editable prompts ----------------------------------------------------
    def _editor(request: Request) -> str | None:
        user = _current_user(request)
        if not settings.can_edit_prompts(user):
            raise HTTPException(403, "Solo los administradores pueden cambiar los prompts.")
        return user

    def _kind(kind: str) -> str:
        if kind not in KINDS:
            raise HTTPException(404, f"Prompt desconocido: {kind}")
        return kind

    @app.get("/api/prompts")
    async def get_prompts(request: Request, input: str = "", output: str = "") -> dict[str, Any]:
        store: PromptStore = request.app.state.prompts
        try:
            states = {k: await store.state(k) for k in KINDS}
        except Exception as exc:
            log.exception("No se pudieron leer los prompts")
            raise HTTPException(502, f"No se pudieron leer los prompts: {exc}") from exc
        inp = input.strip() or settings.default_input
        out = output.strip() or settings.default_output
        return {
            "can_edit": settings.can_edit_prompts(_current_user(request)),
            "placeholders": list(PLACEHOLDERS),
            "factory_id": FACTORY_ID,
            "prompts": states,
            "effective": render(states["base"]["active_text"], states["extra"]["active_text"],
                                inp, out),
        }

    @app.post("/api/prompts/{kind}")
    async def save_prompt(kind: str, body: PromptSave, request: Request) -> dict[str, Any]:
        user = _editor(request)
        try:
            state = await request.app.state.prompts.save(_kind(kind), body.text, body.note, user)
        except PromptError as exc:
            raise HTTPException(400, str(exc)) from exc
        log.info("prompt saved kind=%s version=%s user=%s", kind, state["active"], user)
        return state

    @app.post("/api/prompts/{kind}/activate")
    async def activate_prompt(kind: str, body: PromptActivate, request: Request) -> dict[str, Any]:
        user = _editor(request)
        try:
            state = await request.app.state.prompts.activate(_kind(kind), body.version, user)
        except PromptError as exc:
            raise HTTPException(400, str(exc)) from exc
        log.info("prompt activated kind=%s version=%s user=%s", kind, body.version, user)
        return state

    # ---- direct actions (no AI): call the MCP tools straight away -----------

    def _check(path: str, what: str, *, file_suffix: str | None = None) -> str:
        try:
            loc = parse_location(path, settings.allowed_containers)
        except StorageError as exc:
            raise HTTPException(400, f"{what}: {exc}") from exc
        if file_suffix and not loc.path.lower().endswith(file_suffix):
            raise HTTPException(400, f"{what}: debe ser un archivo {file_suffix}")
        return loc.uri()

    async def _mcp_call(request: Request, tool: str, args: dict[str, Any]) -> Any:
        mcp_ = _require(request.app.state.mcp, "MCP_URL / MCP_API_KEY")
        try:
            return await mcp_.call_tool(tool, args)
        except McpError as exc:
            raise HTTPException(502, str(exc)) from exc

    @app.post("/api/actions/analyze")
    async def action_analyze(body: PackageAction, request: Request) -> dict[str, Any]:
        pkg = _check(body.package_path, "Paquete", file_suffix=".dtsx")
        result = await _mcp_call(request, "analyze_ssis_package", {"package_path": pkg})
        log.info("analyze user=%s package=%s", _current_user(request), pkg)
        return {"analysis": result}

    @app.post("/api/actions/convert")
    async def action_convert(body: PackageAction, request: Request) -> dict[str, Any]:
        pkg = _check(body.package_path, "Paquete", file_suffix=".dtsx")
        out_base = _check(body.output_location or settings.default_output, "Salida")
        name = pkg.rsplit("/", 1)[-1][: -len(".dtsx")]
        out_dir = f"{out_base.rstrip('/')}/{name}"
        convert = await _mcp_call(request, "convert_ssis_package",
                                  {"package_path": pkg, "output_dir": out_dir})
        validate = await _mcp_call(request, "validate_adf_artifacts",
                                   {"artifacts_dir": out_dir})
        log.info("convert user=%s package=%s", _current_user(request), pkg)
        pipeline = convert.get("files", {}).get("pipeline") if isinstance(convert, dict) else None
        return {"output_dir": out_dir, "convert": convert, "validate": validate,
                "pipeline_path": pipeline}

    @app.post("/api/actions/validate")
    async def action_validate(body: ArtifactsAction, request: Request) -> dict[str, Any]:
        folder = _check(body.artifacts_dir, "Carpeta")
        return {"validate": await _mcp_call(request, "validate_adf_artifacts",
                                            {"artifacts_dir": folder})}

    @app.get("/api/pipeline-graph")
    async def get_pipeline_graph(path: str, request: Request) -> dict[str, Any]:
        storage_ = _require(request.app.state.storage, "AZURE_STORAGE_ACCOUNT")
        _check(path, "Pipeline", file_suffix=".json")
        try:
            return await pipeline_graph(storage_, path)
        except ToolError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/files")
    async def list_files(location: str, request: Request, suffix: str = "") -> dict[str, Any]:
        storage_ = _require(request.app.state.storage, "AZURE_STORAGE_ACCOUNT")
        try:
            return {"items": await storage_.list(location, suffix)}
        except StorageError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/upload")
    async def upload(
        request: Request,
        location: str = Form(...),
        file: UploadFile = File(...),
    ) -> dict[str, Any]:
        storage_ = _require(request.app.state.storage, "AZURE_STORAGE_ACCOUNT")
        data = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"El archivo supera {settings.max_upload_mb} MB")
        try:
            path = await storage_.upload(location, file.filename or "", data)
        except StorageError as exc:
            raise HTTPException(400, str(exc)) from exc
        log.info("upload user=%s path=%s", _current_user(request), path)
        return {"path": path}

    @app.get("/api/download")
    async def download(path: str, request: Request) -> Response:
        storage_ = _require(request.app.state.storage, "AZURE_STORAGE_ACCOUNT")
        try:
            name, data = await storage_.download(path)
        except StorageError as exc:
            raise HTTPException(400, str(exc)) from exc
        media = "application/json" if name.endswith(".json") else "application/octet-stream"
        return Response(
            data, media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _current_user(request: Request) -> str | None:
    """User signed in through App Service Authentication (Easy Auth), if enabled."""
    name = request.headers.get("x-ms-client-principal-name")
    if name:
        return name
    principal = request.headers.get("x-ms-client-principal")
    if principal:
        try:
            claims = json.loads(base64.b64decode(principal))
            for c in claims.get("claims", []):
                if c.get("typ") in ("name", "preferred_username"):
                    return c.get("val")
        except (ValueError, TypeError):
            return None
    return None


logging.basicConfig(level=logging.INFO)
# The Azure SDK logs every HTTP request at INFO; keep the App Service log readable.
logging.getLogger("azure").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
app = create_app()
