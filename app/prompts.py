"""
Editable, versioned prompts for the chat agent.

Two prompts make up the system message:

* ``base``  – how the agent works (paths, tools, conventions). It ships with a
  factory version (``FACTORY_BASE``) that can always be restored.
* ``extra`` – additional instructions (tone, scope, company rules). Factory: empty.

Each prompt is stored as one JSON document in Blob Storage
(``<container>/prompts/<kind>.json``) holding its version history and which
version is active. Saving creates a new version; any version — or the factory
one — can be activated again. Security rules (allowed containers, no deploy)
are enforced in code, so no prompt can switch them off.
"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any, Protocol

from azure.core.exceptions import (
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)

KINDS = ("base", "extra")
FACTORY_ID = "factory"
PLACEHOLDERS = ("{{ENTRADA}}", "{{SALIDA}}")
MAX_CHARS = 20_000
MAX_VERSIONS = 50
CACHE_SECONDS = 30

FACTORY_BASE = """Eres un asistente experto en migrar paquetes SSIS a Azure Data Factory (ADF).
Respondes en español, de forma breve y clara.

Tienes herramientas del servidor MCP "ssis-adf-agent" para escanear, analizar, convertir
y validar paquetes. Úsalas en lugar de suponer resultados; nunca inventes datos.

Configuración de esta sesión (úsala siempre salvo que el usuario indique otra ruta):
- Carpeta de ENTRADA (paquetes .dtsx): {{ENTRADA}}
- Carpeta base de SALIDA (artefactos ADF): {{SALIDA}}

Convenciones:
- Para escanear usa scan_ssis_packages con source_type "blob" y path_or_connection "{{ENTRADA}}".
- Un paquete llamado P está en "{{ENTRADA}}/P.dtsx".
- Al convertir P, usa output_dir "{{SALIDA}}/P" (una carpeta por paquete).
- Después de convertir, valida siempre esa carpeta con validate_adf_artifacts.
- No despliegues nada en Azure Data Factory.
- Para ver qué artefactos existen usa list_artifacts; para leer uno, read_artifact.
- Si piden ver, dibujar o explicar el flujo de un pipeline, usa get_pipeline_diagram
  (con el nombre del paquete). La interfaz dibuja el diagrama automáticamente: NO escribas
  diagramas en texto (Mermaid, ASCII); explica el flujo en pocas frases.

Al responder:
- Resume lo generado (pipelines, linked services, datasets, data flows, triggers).
- Destaca los avisos "[MANUAL REVIEW]" y explica qué habría que revisar.
- Si algo falla, explica el error en lenguaje sencillo y sugiere qué hacer.
"""

FACTORY = {"base": FACTORY_BASE, "extra": ""}
EXTRA_HEADER = "Instrucciones adicionales:"


class PromptError(ValueError):
    """Invalid request (shown to the user)."""


def render(base: str, extra: str, input_location: str, output_location: str) -> str:
    """The system message sent to the model."""
    text = (base.replace("{{ENTRADA}}", input_location.rstrip("/"))
                .replace("{{SALIDA}}", output_location.rstrip("/")))
    if extra.strip():
        text = f"{text.rstrip()}\n\n{EXTRA_HEADER}\n{extra.strip()}\n"
    return text


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------- backends

class Conflict(Exception):
    """The document changed since it was read (optimistic concurrency)."""


class Backend(Protocol):
    async def get(self, name: str) -> tuple[bytes | None, str | None]: ...
    async def put(self, name: str, data: bytes, etag: str | None) -> None: ...


class MemoryBackend:
    """In-process storage for local runs and tests."""

    def __init__(self) -> None:
        self.docs: dict[str, tuple[bytes, str]] = {}
        self._n = 0

    async def get(self, name: str) -> tuple[bytes | None, str | None]:
        data, etag = self.docs.get(name, (None, None))
        return data, etag

    async def put(self, name: str, data: bytes, etag: str | None) -> None:
        current = self.docs.get(name, (None, None))[1]
        if current != etag:
            raise Conflict(name)
        self._n += 1
        self.docs[name] = (data, str(self._n))


class BlobBackend:
    """One blob per prompt in a dedicated container (created on first save)."""

    def __init__(self, service: Any, container: str) -> None:
        self.container = service.get_container_client(container)

    async def get(self, name: str) -> tuple[bytes | None, str | None]:
        try:
            stream = await self.container.get_blob_client(name).download_blob()
            return await stream.readall(), stream.properties.etag
        except ResourceNotFoundError:
            return None, None

    async def put(self, name: str, data: bytes, etag: str | None) -> None:
        from azure.core import MatchConditions

        blob = self.container.get_blob_client(name)
        kwargs: dict[str, Any] = (
            {"etag": etag, "match_condition": MatchConditions.IfNotModified} if etag
            else {"match_condition": MatchConditions.IfMissing}
        )
        for attempt in (1, 2):
            try:
                await blob.upload_blob(data, overwrite=True, **kwargs)
                return
            except (ResourceModifiedError, ResourceExistsError) as exc:
                raise Conflict(name) from exc
            except ResourceNotFoundError:
                if attempt == 2:
                    raise
                try:
                    await self.container.create_container()
                except ResourceExistsError:
                    pass


# ---------------------------------------------------------------- store

class PromptStore:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self._cache: tuple[float, tuple[str, str]] | None = None

    @staticmethod
    def _name(kind: str) -> str:
        return f"prompts/{kind}.json"

    @staticmethod
    def _check_kind(kind: str) -> None:
        if kind not in KINDS:
            raise PromptError(f"Prompt desconocido: {kind}")

    async def _read(self, kind: str) -> tuple[dict[str, Any], str | None]:
        data, etag = await self.backend.get(self._name(kind))
        if data is None:
            return {"active": FACTORY_ID, "activated_by": None, "activated_at": None,
                    "versions": []}, None
        return json.loads(data), etag

    async def state(self, kind: str) -> dict[str, Any]:
        """History and active version of one prompt (newest version first)."""
        self._check_kind(kind)
        doc, _ = await self._read(kind)
        active = doc["active"]
        text = next((v["text"] for v in doc["versions"] if v["id"] == active), None)
        if text is None:
            active, text = FACTORY_ID, FACTORY[kind]
        return {
            "kind": kind,
            "active": active,
            "active_text": text,
            "activated_by": doc.get("activated_by"),
            "activated_at": doc.get("activated_at"),
            "factory_text": FACTORY[kind],
            "versions": sorted(doc["versions"], key=lambda v: v["number"], reverse=True),
        }

    async def active_texts(self) -> tuple[str, str]:
        """(base, extra) to build the system message; cached for a few seconds."""
        now = time.monotonic()
        if self._cache and now - self._cache[0] < CACHE_SECONDS:
            return self._cache[1]
        base = (await self.state("base"))["active_text"]
        extra = (await self.state("extra"))["active_text"]
        self._cache = (now, (base, extra))
        return base, extra

    async def _update(self, kind: str, change) -> dict[str, Any]:
        for _ in range(3):
            doc, etag = await self._read(kind)
            change(doc)
            try:
                await self.backend.put(self._name(kind),
                                       json.dumps(doc, ensure_ascii=False, indent=1).encode(),
                                       etag)
            except Conflict:
                continue  # someone saved at the same time: re-read and apply again
            self._cache = None
            return await self.state(kind)
        raise PromptError("Otra persona está guardando este prompt. Inténtalo de nuevo.")

    async def save(self, kind: str, text: str, note: str, author: str | None) -> dict[str, Any]:
        """Store *text* as a new version and make it active."""
        self._check_kind(kind)
        text = text.replace("\r\n", "\n")
        if len(text) > MAX_CHARS:
            raise PromptError(f"El prompt supera {MAX_CHARS} caracteres.")
        if kind == "base":
            if not text.strip():
                raise PromptError("El prompt base no puede estar vacío.")
            missing = [p for p in PLACEHOLDERS if p not in text]
            if missing:
                raise PromptError(
                    f"El prompt base debe incluir {' y '.join(missing)}: ahí se ponen las "
                    "rutas de la sesión. Sin ellas el agente no sabe dónde buscar.")

        def change(doc: dict[str, Any]) -> None:
            number = max((v["number"] for v in doc["versions"]), default=0) + 1
            vid = f"v{number}"
            doc["versions"].append({
                "id": vid, "number": number, "text": text, "note": note.strip()[:200],
                "author": author, "created": _now(),
            })
            doc.update(active=vid, activated_by=author, activated_at=_now())
            # keep the newest versions (never drop the active one)
            if len(doc["versions"]) > MAX_VERSIONS:
                doc["versions"] = sorted(doc["versions"], key=lambda v: v["number"])
                doc["versions"] = doc["versions"][-MAX_VERSIONS:]

        return await self._update(kind, change)

    async def activate(self, kind: str, version: str, author: str | None) -> dict[str, Any]:
        """Make a stored version (or ``factory``) the active one."""
        self._check_kind(kind)

        def change(doc: dict[str, Any]) -> None:
            if version != FACTORY_ID and not any(v["id"] == version for v in doc["versions"]):
                raise PromptError(f"No existe la versión {version}.")
            doc.update(active=version, activated_by=author, activated_at=_now())

        return await self._update(kind, change)
