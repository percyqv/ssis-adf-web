"""Direct actions (no AI), pipeline graph endpoint and the web's local tools."""
from __future__ import annotations

from pathlib import Path

from tests.test_app import SESSION, FakeMcp, FakeStorage, ScriptedModel, call, client

DATA = Path(__file__).parent / "data"
OUT = "az://adf-artifacts/Finanzas/CargaClientes"
PIPELINE = f"{OUT}/pipeline/PL_CargaClientes.json"


class ActionsMcp(FakeMcp):
    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "analyze_ssis_package":
            return {"package_name": "CargaClientes",
                    "complexity": {"score": 42, "effort_estimate": "Medium", "total_tasks": 4}}
        if name == "convert_ssis_package":
            pipeline = f"{arguments['output_dir']}/pipeline/PL_CargaClientes.json"
            return {"artifacts_generated": {"pipelines": 1}, "files": {"pipeline": pipeline}}
        if name == "validate_adf_artifacts":
            return {"status": "valid"}
        return {}


def storage_with_pipeline() -> FakeStorage:
    s = FakeStorage()
    s.blobs[PIPELINE] = (DATA / "PL_CargaClientes.json").read_bytes()
    return s


def test_analyze_and_convert_call_the_mcp_without_the_model() -> None:
    mcp, model = ActionsMcp(), ScriptedModel()
    with client(model, mcp=mcp) as c:
        pkg = "az://ssis-packages/Finanzas/CargaClientes.dtsx"
        r = c.post("/api/actions/analyze", json={"package_path": pkg})
        assert r.status_code == 200, r.text
        assert r.json()["analysis"]["complexity"]["score"] == 42

        r = c.post("/api/actions/convert",
                   json={"package_path": pkg, "output_location": "az://adf-artifacts/Finanzas"})
        body = r.json()
        assert body["output_dir"] == OUT
        assert body["pipeline_path"] == PIPELINE
        assert body["validate"]["status"] == "valid"
    assert [n for n, _ in mcp.calls] == [
        "analyze_ssis_package", "convert_ssis_package", "validate_adf_artifacts"]
    assert mcp.calls[1][1]["output_dir"] == OUT
    assert model.seen == []  # no AI involved


def test_actions_reject_paths_outside_allowed_containers() -> None:
    mcp = ActionsMcp()
    with client(ScriptedModel(), mcp=mcp) as c:
        for bad in ("az://secreto/x.dtsx", "az://ssis-packages/Finanzas/x.txt", "C:/x.dtsx"):
            assert c.post("/api/actions/analyze", json={"package_path": bad}).status_code == 400
        r = c.post("/api/actions/convert", json={
            "package_path": "az://ssis-packages/F/a.dtsx", "output_location": "az://otro/F"})
        assert r.status_code == 400
    assert mcp.calls == []


def test_pipeline_graph_endpoint() -> None:
    with client(ScriptedModel(), storage=storage_with_pipeline()) as c:
        g = c.get("/api/pipeline-graph", params={"path": PIPELINE}).json()
        assert g["name"] == "PL_CargaClientes" and g["path"] == PIPELINE
        assert g["stats"]["activities"] == 4
        missing = c.get("/api/pipeline-graph", params={"path": f"{OUT}/pipeline/X.json"})
        assert missing.status_code == 400
        assert c.get("/api/pipeline-graph",
                     params={"path": "az://secreto/p.json"}).status_code == 400


def test_chat_diagram_tool_returns_attachment() -> None:
    model = ScriptedModel(
        {"content": None, "tool_calls": [call("get_pipeline_diagram", package="CargaClientes")]},
        {"content": "Este es el flujo.", "tool_calls": []},
    )
    with client(model, storage=storage_with_pipeline()) as c:
        r = c.post("/api/chat", json={**SESSION, "messages": [
            {"role": "user", "content": "Muéstrame el flujo de CargaClientes"}]})
        body = r.json()
    assert r.status_code == 200, body
    assert body["reply"] == "Este es el flujo."
    [att] = body["attachments"]
    assert att["type"] == "pipeline_graph" and att["path"] == PIPELINE
    assert att["graph"]["stats"]["activities"] == 4
    # the model received a text description, not the raw graph
    tool_msg = model.seen[1][-1]
    assert tool_msg["role"] == "tool" and "Cargar Clientes" in tool_msg["content"]
    names = {t["function"]["name"] for t in model.tools}
    assert {"get_pipeline_diagram", "list_artifacts", "read_artifact"} <= names


def test_diagram_tool_rejects_bad_package_names() -> None:
    model = ScriptedModel(
        {"content": None, "tool_calls": [call("get_pipeline_diagram", package="../secreto")]},
        {"content": "No pude.", "tool_calls": []},
    )
    with client(model, storage=storage_with_pipeline()) as c:
        body = c.post("/api/chat", json={**SESSION, "messages": [
            {"role": "user", "content": "flujo"}]}).json()
    assert body["attachments"] == []
    assert not body["steps"][0]["ok"]
