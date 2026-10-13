"""
Chat agent: Azure OpenAI chooses which MCP tools to call (function calling).

The browser sends the conversation (user/assistant text only) plus the session
settings (input and output locations). Every request is independent: the agent
runs the tool loop and returns the final answer and the list of tool calls made.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .mcp_client import McpClient, McpError, McpTool

_AZ_PREFIX = re.compile(r"^az://[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])(?:/[^\s<>\"'`]*)?$")
_MAX_TOOL_RESULT_CHARS = 12_000


class ChatModel(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Return an assistant message dict: {"content": str|None, "tool_calls": [...]}."""


@dataclass
class SessionSettings:
    input_location: str
    output_location: str

    def validate(self, allowed_containers: list[str]) -> None:
        for label, value in (("entrada", self.input_location), ("salida", self.output_location)):
            if not _AZ_PREFIX.match(value or ""):
                raise ValueError(
                    f"La ruta de {label} debe tener la forma az://<contenedor>/<carpeta>."
                )
            container = value[5:].split("/", 1)[0]
            if allowed_containers and container not in allowed_containers:
                raise ValueError(
                    f"El contenedor '{container}' no está permitido "
                    f"(permitidos: {', '.join(allowed_containers)})."
                )


@dataclass
class ToolStep:
    tool: str
    arguments: dict[str, Any]
    ok: bool
    summary: str


@dataclass
class AgentResult:
    reply: str
    steps: list[ToolStep] = field(default_factory=list)


def system_prompt(settings: SessionSettings) -> str:
    inp = settings.input_location.rstrip("/")
    out = settings.output_location.rstrip("/")
    return f"""Eres un asistente experto en migrar paquetes SSIS a Azure Data Factory (ADF).
Respondes en español, de forma breve y clara.

Tienes herramientas del servidor MCP "ssis-adf-agent" para escanear, analizar, convertir
y validar paquetes. Úsalas en lugar de suponer resultados; nunca inventes datos.

Configuración de esta sesión (úsala siempre salvo que el usuario indique otra ruta):
- Carpeta de ENTRADA (paquetes .dtsx): {inp}
- Carpeta base de SALIDA (artefactos ADF): {out}

Convenciones:
- Para escanear usa scan_ssis_packages con source_type "blob" y path_or_connection "{inp}".
- Un paquete llamado P está en "{inp}/P.dtsx".
- Al convertir P, usa output_dir "{out}/P" (una carpeta por paquete).
- Después de convertir, valida siempre esa carpeta con validate_adf_artifacts.
- No despliegues nada en Azure Data Factory.

Al responder:
- Resume lo generado (pipelines, linked services, datasets, data flows, triggers).
- Destaca los avisos "[MANUAL REVIEW]" y explica qué habría que revisar.
- Si algo falla, explica el error en lenguaje sencillo y sugiere qué hacer.
"""


def _tool_schema(tool: McpTool) -> dict[str, Any]:
    params = tool.input_schema or {"type": "object", "properties": {}}
    params.setdefault("type", "object")
    params.setdefault("properties", {})
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description[:1024],
            "parameters": params,
        },
    }


def _summarize(result: Any) -> str:
    if isinstance(result, dict):
        if "artifacts_generated" in result:
            gen = result["artifacts_generated"]
            parts = [f"{k}={v}" for k, v in gen.items() if v]
            warn = result.get("manual_review_required", 0)
            return f"Generado: {', '.join(parts) or 'nada'}; revisión manual: {warn}"
        if "status" in result:
            return f"Estado: {result['status']}"
        if "found" in result:
            return f"Paquetes encontrados: {result['found']}"
        if "complexity" in result:
            c = result["complexity"]
            return f"Complejidad {c.get('score')} ({c.get('effort_estimate')}), " \
                   f"brechas: {result.get('gap_count', 0)}"
    text = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
    return text[:200]


_PATH_ARGS = ("package_path", "output_dir", "artifacts_dir", "path_or_connection",
              "esi_tables_path", "schema_remap_path", "shared_artifacts_dir")


def _path_problem(args: dict[str, Any], allowed_containers: list[str]) -> str | None:
    """Only allow Blob locations in the allowed containers (no local paths, no git/sql)."""
    if args.get("source_type") not in (None, "blob"):
        return "solo se permite source_type 'blob'"
    for key in _PATH_ARGS:
        value = args.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.startswith("az://"):
            return f"'{key}' debe ser una ruta az://<contenedor>/<ruta>"
        container = value[5:].split("/", 1)[0]
        if allowed_containers and container not in allowed_containers:
            return f"el contenedor '{container}' no está permitido"
        if ".." in value.split("/"):
            return f"'{key}' no es una ruta válida"
    return None


class Agent:
    def __init__(
        self,
        model: ChatModel,
        mcp: McpClient,
        allowed_tools: list[str],
        max_steps: int = 8,
        allowed_containers: list[str] | None = None,
    ) -> None:
        self.model = model
        self.mcp = mcp
        self.allowed_tools = set(allowed_tools)
        self.max_steps = max_steps
        self.allowed_containers = allowed_containers or []

    async def run(
        self, conversation: list[dict[str, str]], settings: SessionSettings
    ) -> AgentResult:
        tools = [t for t in await self.mcp.list_tools() if t.name in self.allowed_tools]
        tool_schemas = [_tool_schema(t) for t in tools]
        allowed = {t.name for t in tools}

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(settings)}
        ]
        for m in conversation[-20:]:
            if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
                messages.append({"role": m["role"], "content": m["content"][:8000]})

        steps: list[ToolStep] = []
        for _ in range(self.max_steps):
            msg = await self.model.complete(messages, tool_schemas)
            calls = msg.get("tool_calls") or []
            if not calls:
                return AgentResult(reply=msg.get("content") or "", steps=steps)

            messages.append(
                {"role": "assistant", "content": msg.get("content"), "tool_calls": calls}
            )
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except ValueError:
                    args = {}
                problem = (
                    f"la herramienta '{name}' no está disponible" if name not in allowed
                    else _path_problem(args, self.allowed_containers)
                )
                if problem:
                    content = f"Error: {problem}."
                    steps.append(ToolStep(name, args, False, problem))
                else:
                    try:
                        result = await self.mcp.call_tool(name, args)
                        content = (
                            result if isinstance(result, str)
                            else json.dumps(result, ensure_ascii=False)
                        )
                        steps.append(ToolStep(name, args, True, _summarize(result)))
                    except McpError as exc:
                        content = f"Error: {exc}"
                        steps.append(ToolStep(name, args, False, str(exc)[:300]))
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": content[:_MAX_TOOL_RESULT_CHARS],
                })

        return AgentResult(
            reply="Me detuve porque la tarea necesitaba demasiados pasos. "
                  "Intenta pedirla por partes.",
            steps=steps,
        )
