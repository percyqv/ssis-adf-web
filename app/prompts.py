"""
Editable, versioned prompts for the chat agent.

Two prompts make up the system message:

* ``base``  – how the agent works (paths, tools, conventions). It ships with a
  factory version (``FACTORY_BASE``) that can always be restored.
* ``extra`` – additional instructions (tone, scope, company rules). Factory: empty.

Versions live in a SQLite database (one file). On App Service it is kept under
``/home/data``, which survives restarts and redeploys. Saving creates a new
version; any version — or the factory one — can be activated again. Security
rules (allowed containers, no deploy) are enforced in code, so no prompt can
switch them off.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

KINDS = ("base", "extra")
FACTORY_ID = "factory"
PLACEHOLDERS = ("{{ENTRADA}}", "{{SALIDA}}")
MAX_CHARS = 20_000
MAX_VERSIONS = 50

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


def default_db_path() -> str:
    """/home persists on App Service (Linux); locally use ./data."""
    if os.environ.get("PROMPTS_DB"):
        return os.environ["PROMPTS_DB"]
    if os.environ.get("WEBSITE_SITE_NAME"):  # running on App Service
        return "/home/data/ssis-adf-web/prompts.db"
    return str(Path("data") / "prompts.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_version (
    kind    TEXT NOT NULL,
    number  INTEGER NOT NULL,
    text    TEXT NOT NULL,
    note    TEXT NOT NULL DEFAULT '',
    author  TEXT,
    created TEXT NOT NULL,
    PRIMARY KEY (kind, number)
);
CREATE TABLE IF NOT EXISTS prompt_active (
    kind         TEXT PRIMARY KEY,
    version      TEXT NOT NULL,          -- 'v<number>' or 'factory'
    activated_by TEXT,
    activated_at TEXT NOT NULL
);
"""


class PromptStore:
    """Prompt versions in SQLite. Calls run in a thread so they never block the event loop."""

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One shared connection; a lock serialises access (the app runs one process).
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        # /home on App Service is a network share: no WAL there (it needs shared memory).
        self._db.execute("PRAGMA journal_mode=DELETE")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(_SCHEMA)
        self._lock = asyncio.Lock()

    def close(self) -> None:
        self._db.close()

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    @staticmethod
    def _check_kind(kind: str) -> None:
        if kind not in KINDS:
            raise PromptError(f"Prompt desconocido: {kind}")

    # ---- reads
    def _state(self, kind: str) -> dict[str, Any]:
        rows = self._db.execute(
            "SELECT number, text, note, author, created FROM prompt_version "
            "WHERE kind = ? ORDER BY number DESC", (kind,)).fetchall()
        versions = [{"id": f"v{r['number']}", **dict(r)} for r in rows]
        act = self._db.execute("SELECT version, activated_by, activated_at FROM prompt_active "
                               "WHERE kind = ?", (kind,)).fetchone()
        active = act["version"] if act else FACTORY_ID
        text = next((v["text"] for v in versions if v["id"] == active), None)
        if text is None:
            active, text = FACTORY_ID, FACTORY[kind]
        return {
            "kind": kind,
            "active": active,
            "active_text": text,
            "activated_by": act["activated_by"] if act else None,
            "activated_at": act["activated_at"] if act else None,
            "factory_text": FACTORY[kind],
            "versions": versions,
        }

    async def state(self, kind: str) -> dict[str, Any]:
        """History (newest first) and active version of one prompt."""
        self._check_kind(kind)
        return await self._run(self._state, kind)

    async def active_texts(self) -> tuple[str, str]:
        """(base, extra) used to build the system message."""
        def read() -> tuple[str, str]:
            return self._state("base")["active_text"], self._state("extra")["active_text"]
        return await self._run(read)

    # ---- writes
    def _activate(self, kind: str, version: str, author: str | None) -> None:
        self._db.execute(
            "INSERT INTO prompt_active (kind, version, activated_by, activated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(kind) DO UPDATE SET version = excluded.version, "
            "activated_by = excluded.activated_by, activated_at = excluded.activated_at",
            (kind, version, author, _now()))

    def _save(self, kind: str, text: str, note: str, author: str | None) -> dict[str, Any]:
        with self._db:  # one transaction
            self._db.execute("BEGIN IMMEDIATE")
            number = self._db.execute(
                "SELECT COALESCE(MAX(number), 0) + 1 FROM prompt_version WHERE kind = ?",
                (kind,)).fetchone()[0]
            self._db.execute(
                "INSERT INTO prompt_version (kind, number, text, note, author, created) "
                "VALUES (?, ?, ?, ?, ?, ?)", (kind, number, text, note, author, _now()))
            self._activate(kind, f"v{number}", author)
            # keep the newest versions only
            self._db.execute(
                "DELETE FROM prompt_version WHERE kind = ? AND number <= ?",
                (kind, number - MAX_VERSIONS))
        return self._state(kind)

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
        return await self._run(self._save, kind, text, note.strip()[:200], author)

    async def activate(self, kind: str, version: str, author: str | None) -> dict[str, Any]:
        """Make a stored version (or ``factory``) the active one."""
        self._check_kind(kind)

        def do() -> dict[str, Any]:
            if version != FACTORY_ID:
                number = version[1:] if version.startswith("v") else ""
                exists = number.isdigit() and self._db.execute(
                    "SELECT 1 FROM prompt_version WHERE kind = ? AND number = ?",
                    (kind, int(number))).fetchone()
                if not exists:
                    raise PromptError(f"No existe la versión {version}.")
            self._activate(kind, version, author)
            return self._state(kind)

        return await self._run(do)
