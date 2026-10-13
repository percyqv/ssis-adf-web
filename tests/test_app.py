"""Tests with a scripted chat model and fake MCP / Storage (no network)."""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent import Agent, SessionSettings, system_prompt
from app.config import Settings
from app.main import create_app
from app.mcp_client import McpError, McpTool
from app.storage import StorageError, parse_location

SETTINGS = Settings(
    mcp_url="https://mcp.test/mcp",
    mcp_api_key="k" * 30,
    openai_endpoint="https://oai.test",
    openai_deployment="gpt",
    storage_account="sttest",
    allowed_containers=["ssis-packages", "adf-artifacts"],
    allowed_tools=["scan_ssis_packages", "convert_ssis_package", "validate_adf_artifacts"],
)
SESSION = {"input_location": "az://ssis-packages/Finanzas",
           "output_location": "az://adf-artifacts/Finanzas"}


def call(name: str, **args: Any) -> dict[str, Any]:
    return {"id": f"call_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class ScriptedModel:
    """Returns the given assistant messages in order and records what it saw."""

    def __init__(self, *turns: dict[str, Any]) -> None:
        self.turns = list(turns)
        self.seen: list[list[dict[str, Any]]] = []
        self.tools: list[dict[str, Any]] = []

    async def complete(self, messages, tools):
        self.seen.append(json.loads(json.dumps(messages)))
        self.tools = tools
        return self.turns.pop(0)


class FakeMcp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_tools(self):
        return [
            McpTool("scan_ssis_packages", "scan", {"type": "object", "properties": {}}),
            McpTool("convert_ssis_package", "convert", {"type": "object", "properties": {}}),
            McpTool("validate_adf_artifacts", "validate", {"type": "object", "properties": {}}),
            McpTool("deploy_to_adf", "deploy", {"type": "object", "properties": {}}),
        ]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "convert_ssis_package":
            if arguments["package_path"].endswith("Falla.dtsx"):
                raise McpError("Blob not found: az://ssis-packages/Finanzas/Falla.dtsx")
            return {"artifacts_generated": {"pipelines": 1, "linked_services": 2},
                    "manual_review_required": 0}
        if name == "validate_adf_artifacts":
            return {"status": "valid"}
        return {"found": 1, "packages": [{"name": "CargaVentas"}]}


class FakeStorage:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def list(self, location, suffix=""):
        loc = parse_location(location, SETTINGS.allowed_containers)
        return [{"path": p, "name": p.split("/")[-1], "size": len(b), "modified": None}
                for p, b in sorted(self.blobs.items())
                if p.startswith(loc.uri()) and p.endswith(suffix)]

    async def upload(self, location, filename, data):
        if not filename.endswith(".dtsx"):
            raise StorageError("Solo se permiten archivos .dtsx con nombre simple")
        path = f"{location.rstrip('/')}/{filename}"
        self.blobs[path] = data
        return path

    async def download(self, path):
        if path not in self.blobs:
            raise StorageError("El archivo no existe")
        return path.split("/")[-1], self.blobs[path]

    async def close(self):
        pass


def client(model, mcp=None, storage=None) -> TestClient:
    return TestClient(create_app(SETTINGS, model=model, mcp=mcp or FakeMcp(),
                                 storage=storage or FakeStorage()))


# ---------------------------------------------------------------- agent

def test_agent_runs_tool_loop_and_hides_disallowed_tools() -> None:
    mcp = FakeMcp()
    model = ScriptedModel(
        {"content": None, "tool_calls": [call(
            "convert_ssis_package",
            package_path="az://ssis-packages/Finanzas/CargaVentas.dtsx",
            output_dir="az://adf-artifacts/Finanzas/CargaVentas")]},
        {"content": None, "tool_calls": [call(
            "validate_adf_artifacts", artifacts_dir="az://adf-artifacts/Finanzas/CargaVentas")]},
        {"content": "Listo: 1 pipeline y 2 linked services.", "tool_calls": []},
    )
    with client(model, mcp) as c:
        r = c.post("/api/chat", json={**SESSION, "messages": [
            {"role": "user", "content": "Convierte CargaVentas"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reply"].startswith("Listo")
    assert [s["tool"] for s in body["steps"]] == ["convert_ssis_package", "validate_adf_artifacts"]
    assert all(s["ok"] for s in body["steps"])
    assert [n for n, _ in mcp.calls] == ["convert_ssis_package", "validate_adf_artifacts"]
    # deploy_to_adf is never offered to the model
    assert "deploy_to_adf" not in {t["function"]["name"] for t in model.tools}
    # the session paths are in the system prompt; tool results are fed back
    assert "az://ssis-packages/Finanzas" in model.seen[0][0]["content"]
    assert model.seen[1][-1]["role"] == "tool"


@pytest.mark.parametrize(
    "args, problem",
    [
        ({"package_path": "C:/secret/x.dtsx", "output_dir": "az://adf-artifacts/x"}, "az://"),
        ({"package_path": "az://otro/x.dtsx", "output_dir": "az://adf-artifacts/x"},
         "no está permitido"),
        ({"package_path": "az://ssis-packages/../x.dtsx", "output_dir": "az://adf-artifacts/x"},
         "no es una ruta válida"),
    ],
)
def test_agent_refuses_paths_outside_allowed_containers(args, problem) -> None:
    mcp = FakeMcp()
    model = ScriptedModel(
        {"content": None, "tool_calls": [call("convert_ssis_package", **args)]},
        {"content": "No pude.", "tool_calls": []},
    )
    with client(model, mcp) as c:
        body = c.post("/api/chat", json={**SESSION, "messages": [
            {"role": "user", "content": "x"}]}).json()
    assert mcp.calls == []
    assert body["steps"][0]["ok"] is False and problem in body["steps"][0]["summary"]


def test_agent_reports_mcp_errors_to_the_model() -> None:
    model = ScriptedModel(
        {"content": None, "tool_calls": [call(
            "convert_ssis_package", package_path="az://ssis-packages/Finanzas/Falla.dtsx",
            output_dir="az://adf-artifacts/Finanzas/Falla")]},
        {"content": "El paquete no existe.", "tool_calls": []},
    )
    with client(model) as c:
        body = c.post("/api/chat", json={**SESSION, "messages": [
            {"role": "user", "content": "x"}]}).json()
    assert body["steps"][0]["ok"] is False
    assert "Blob not found" in model.seen[1][-1]["content"]


def test_agent_stops_after_max_steps() -> None:
    loop = {"content": None, "tool_calls": [call(
        "scan_ssis_packages", source_type="blob", path_or_connection="az://ssis-packages/F")]}
    model = ScriptedModel(*([loop] * SETTINGS.max_agent_steps))
    with client(model) as c:
        body = c.post("/api/chat", json={**SESSION, "messages": [
            {"role": "user", "content": "x"}]}).json()
    assert "demasiados pasos" in body["reply"]


@pytest.mark.parametrize("bad", ["ssis-packages/x", "az://otro/x", "az://ssis-packages/a b"])
def test_chat_rejects_invalid_session_paths(bad: str) -> None:
    with client(ScriptedModel()) as c:
        r = c.post("/api/chat", json={"input_location": bad,
                                      "output_location": SESSION["output_location"],
                                      "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 400 and "error" in r.json()


def test_system_prompt_conventions() -> None:
    p = system_prompt(SessionSettings("az://ssis-packages/F/", "az://adf-artifacts/F"))
    assert '"az://ssis-packages/F/P.dtsx"' in p and '"az://adf-artifacts/F/P"' in p
    assert "No despliegues" in p


# ---------------------------------------------------------------- files

def test_upload_list_download_roundtrip() -> None:
    storage = FakeStorage()
    with client(ScriptedModel(), storage=storage) as c:
        r = c.post("/api/upload", data={"location": "az://ssis-packages/Finanzas"},
                   files={"file": ("Carga.dtsx", b"<x/>", "application/xml")})
        assert r.status_code == 200, r.text
        path = r.json()["path"]
        items = c.get("/api/files", params={"location": "az://ssis-packages/Finanzas",
                                            "suffix": ".dtsx"}).json()["items"]
        assert [i["name"] for i in items] == ["Carga.dtsx"]
        d = c.get("/api/download", params={"path": path})
        assert d.content == b"<x/>" and "attachment" in d.headers["content-disposition"]

        bad = c.post("/api/upload", data={"location": "az://ssis-packages/Finanzas"},
                     files={"file": ("x.exe", b"MZ", "application/octet-stream")})
        assert bad.status_code == 400
        assert c.get("/api/files", params={"location": "az://secreto/x"}).status_code == 400


def test_parse_location_rules() -> None:
    allowed = ["ssis-packages"]
    assert parse_location("az://ssis-packages/F/", allowed).path == "F"
    for bad in ("https://x", "az://otro/F", "az://ssis-packages/../F"):
        with pytest.raises(StorageError):
            parse_location(bad, allowed)


def test_index_config_and_user_header() -> None:
    with client(ScriptedModel()) as c:
        assert "SSIS" in c.get("/").text
        cfg = c.get("/api/config", headers={"X-MS-CLIENT-PRINCIPAL-NAME": "ana@inetum.com"}).json()
        assert cfg["user"] == "ana@inetum.com"
        assert cfg["default_input"].startswith("az://") and cfg["missing_settings"] == []
        assert c.get("/healthz").json()["status"] == "ok"


def test_missing_configuration_returns_503() -> None:
    app = create_app(Settings(allowed_containers=["ssis-packages", "adf-artifacts"]))
    with TestClient(app) as c:
        r = c.post("/api/chat", json={**SESSION, "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 503
        assert "MCP_URL" in c.get("/api/config").json()["missing_settings"]


def test_agent_class_is_usable_directly() -> None:
    import asyncio

    model = ScriptedModel({"content": "hola", "tool_calls": []})
    agent = Agent(model, FakeMcp(), SETTINGS.allowed_tools)
    res = asyncio.run(agent.run([{"role": "user", "content": "hola"}], SessionSettings(**SESSION)))
    assert res.reply == "hola" and res.steps == []


def test_token_scope_by_endpoint() -> None:
    from app.llm import token_scope

    assert token_scope("https://x.services.ai.azure.com") == "https://ai.azure.com/.default"
    assert token_scope("https://x.openai.azure.com") == "https://cognitiveservices.azure.com/.default"
    assert token_scope("https://x.cognitiveservices.azure.com", "custom") == "custom"
