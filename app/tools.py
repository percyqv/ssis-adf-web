"""Tools implemented by the web app (Storage reads and pipeline diagrams).

They read the Storage Account directly, so simple questions ("which artifacts are
there?", "show me the flow") do not need a round trip to the MCP server.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .agent import LocalTool, LocalToolResult, SessionSettings, ToolError
from .graph import build_pipeline_graph, describe_graph, load_pipeline
from .storage import Storage, StorageError

_MAX_ARTIFACT_CHARS = 15_000


def _safe_folder(name: str) -> str:
    """A package name as used in output folders (the converter keeps the .dtsx stem)."""
    name = name.strip().removesuffix(".dtsx")
    if not name or not re.match(r"^[\w .\-()]+$", name) or ".." in name:
        raise ToolError(f"nombre de paquete no válido: {name!r}")
    return name


async def find_pipeline(storage: Storage, output_location: str, package: str) -> str:
    """Path of the pipeline JSON generated for *package* under *output_location*."""
    folder = f"{output_location.rstrip('/')}/{_safe_folder(package)}/pipeline"
    try:
        items = await storage.list(folder, suffix=".json")
    except StorageError as exc:
        raise ToolError(str(exc)) from exc
    if not items:
        raise ToolError(
            f"no hay pipeline generado para '{package}' en {folder}. Conviértelo primero."
        )
    return items[0]["path"]


async def pipeline_graph(storage: Storage, path: str) -> dict[str, Any]:
    try:
        _, data = await storage.download(path)
        graph = build_pipeline_graph(load_pipeline(data))
    except (StorageError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    graph["path"] = path
    return graph


def build_local_tools(storage: Storage, session: SessionSettings) -> list[LocalTool]:
    out = session.output_location.rstrip("/")

    async def list_artifacts(args: dict[str, Any]) -> LocalToolResult:
        location = args.get("location") or out
        try:
            items = await storage.list(location, suffix=".json")
        except StorageError as exc:
            raise ToolError(str(exc)) from exc
        names = [i["name"] for i in items]
        content = json.dumps({"location": location, "count": len(names), "artifacts": names},
                             ensure_ascii=False)
        return LocalToolResult(content, f"Artefactos: {len(names)}")

    async def read_artifact(args: dict[str, Any]) -> LocalToolResult:
        path = args.get("path") or ""
        if not path.endswith(".json"):
            raise ToolError("solo se pueden leer artefactos .json")
        try:
            name, data = await storage.download(path)
        except StorageError as exc:
            raise ToolError(str(exc)) from exc
        text = data.decode("utf-8-sig", errors="replace")
        if len(text) > _MAX_ARTIFACT_CHARS:
            text = text[:_MAX_ARTIFACT_CHARS] + "\n… (recortado)"
        return LocalToolResult(text, f"Leído {name}")

    async def get_pipeline_diagram(args: dict[str, Any]) -> LocalToolResult:
        path = args.get("path")
        if not path:
            if not args.get("package"):
                raise ToolError("indica 'package' (nombre del paquete) o 'path'")
            path = await find_pipeline(storage, out, args["package"])
        graph = await pipeline_graph(storage, path)
        stats = graph["stats"]
        return LocalToolResult(
            content=describe_graph(graph) + "\n(El diagrama ya se muestra al usuario.)",
            summary=f"Diagrama de {graph['name']}: {stats['activities']} actividades",
            attachment={"type": "pipeline_graph", "path": path, "graph": graph},
        )

    return [
        LocalTool(
            "list_artifacts",
            "Lista los artefactos ADF (.json) generados en una carpeta del Storage. "
            f"Por defecto, la carpeta de salida de la sesión ({out}).",
            {"type": "object", "properties": {"location": {
                "type": "string", "description": "Carpeta az://contenedor/ruta (opcional)."}}},
            list_artifacts,
        ),
        LocalTool(
            "read_artifact",
            "Lee el contenido JSON de un artefacto ADF generado (pipeline, dataset, "
            "linked service, data flow o trigger).",
            {"type": "object", "properties": {"path": {
                "type": "string", "description": "Ruta az://… del archivo .json."}},
             "required": ["path"]},
            read_artifact,
        ),
        LocalTool(
            "get_pipeline_diagram",
            "Muestra al usuario el diagrama interactivo del pipeline ADF de un paquete ya "
            "convertido y devuelve su estructura (actividades y dependencias).",
            {"type": "object", "properties": {
                "package": {"type": "string",
                            "description": "Nombre del paquete, p. ej. 'CargaVentas'."},
                "path": {"type": "string",
                         "description": "Alternativa: ruta az://… del pipeline .json."}}},
            get_pipeline_diagram,
        ),
    ]
