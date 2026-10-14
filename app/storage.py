"""Direct Blob Storage operations for the web UI: upload, list and download."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob.aio import BlobServiceClient

_NAME = re.compile(r"^[A-Za-z0-9 _.\-()]+$")


class StorageError(ValueError):
    pass


@dataclass(frozen=True)
class Location:
    container: str
    path: str  # no leading/trailing slash

    def child(self, name: str) -> str:
        return f"{self.path}/{name}" if self.path else name

    def uri(self, blob: str | None = None) -> str:
        return f"az://{self.container}/{blob if blob is not None else self.path}".rstrip("/")


def parse_location(value: str, allowed_containers: list[str]) -> Location:
    v = (value or "").strip()
    if not v.startswith("az://"):
        raise StorageError("La ruta debe empezar por az://<contenedor>/")
    container, _, path = v[5:].partition("/")
    path = path.strip("/")
    if not container or (allowed_containers and container not in allowed_containers):
        raise StorageError(f"Contenedor no permitido: '{container}'")
    if ".." in path.split("/"):
        raise StorageError("Ruta no válida")
    return Location(container, path)


class Storage:
    def __init__(self, account: str, allowed_containers: list[str]) -> None:
        self.allowed = allowed_containers
        conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn:  # local tests (Azurite)
            self._svc = BlobServiceClient.from_connection_string(conn)
        else:
            from azure.identity.aio import DefaultAzureCredential

            self._svc = BlobServiceClient(
                f"https://{account}.blob.core.windows.net", credential=DefaultAzureCredential()
            )

    @property
    def service(self) -> BlobServiceClient:
        return self._svc

    async def list(self, location: str, suffix: str = "") -> list[dict[str, Any]]:
        loc = parse_location(location, self.allowed)
        container = self._svc.get_container_client(loc.container)
        prefix = f"{loc.path}/" if loc.path else None
        items: list[dict[str, Any]] = []
        if not await container.exists():
            raise StorageError(f"El contenedor '{loc.container}' no existe")
        async for blob in container.list_blobs(name_starts_with=prefix, include=["metadata"]):
            md = blob.metadata or {}
            if str(md.get("hdi_isfolder", "")).lower() == "true" or blob.name.endswith("/"):
                continue
            if suffix and not blob.name.lower().endswith(suffix.lower()):
                continue
            items.append({
                "path": loc.uri(blob.name),
                "name": blob.name[len(prefix or ""):],
                "size": blob.size,
                "modified": blob.last_modified.isoformat() if blob.last_modified else None,
            })
        return sorted(items, key=lambda i: i["name"])

    async def upload(self, location: str, filename: str, data: bytes) -> str:
        loc = parse_location(location, self.allowed)
        name = os.path.basename(filename or "")
        if not name.lower().endswith(".dtsx") or not _NAME.match(name):
            raise StorageError("Solo se permiten archivos .dtsx con nombre simple")
        blob = loc.child(name)
        try:
            await self._svc.get_blob_client(loc.container, blob).upload_blob(data, overwrite=True)
        except ResourceNotFoundError as exc:
            raise StorageError(f"El contenedor '{loc.container}' no existe") from exc
        return loc.uri(blob)

    async def download(self, path: str) -> tuple[str, bytes]:
        loc = parse_location(path, self.allowed)
        if not loc.path:
            raise StorageError("Indica un archivo, no un contenedor")
        try:
            stream = await self._svc.get_blob_client(loc.container, loc.path).download_blob()
            data = await stream.readall()
        except ResourceNotFoundError as exc:
            raise StorageError("El archivo no existe") from exc
        return os.path.basename(loc.path), data

    async def close(self) -> None:
        await self._svc.close()
