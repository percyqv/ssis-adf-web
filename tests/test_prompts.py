"""Editable, versioned prompts."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.prompts import FACTORY_BASE, PromptError, PromptStore, render
from tests.test_app import SESSION, SETTINGS, FakeMcp, FakeStorage, ScriptedModel

BASE_V1 = "Eres un experto. Entrada {{ENTRADA}}, salida {{SALIDA}}."


def run(coro):
    return asyncio.run(coro)


def test_versions_restore_and_factory() -> None:
    store = PromptStore(":memory:")
    s = run(store.state("base"))
    assert s["active"] == "factory" and s["active_text"] == FACTORY_BASE and s["versions"] == []

    run(store.save("base", BASE_V1, "primera", "ana@x.com"))
    s = run(store.save("base", BASE_V1 + " Sé breve.", "", "luis@x.com"))
    assert s["active"] == "v2" and [v["id"] for v in s["versions"]] == ["v2", "v1"]
    assert s["versions"][1]["note"] == "primera" and s["versions"][1]["author"] == "ana@x.com"

    s = run(store.activate("base", "v1", "ana@x.com"))
    assert s["active"] == "v1" and s["active_text"] == BASE_V1

    s = run(store.activate("base", "factory", "ana@x.com"))
    assert s["active_text"] == FACTORY_BASE
    assert len(s["versions"]) == 2  # restoring never deletes history

    with pytest.raises(PromptError):
        run(store.activate("base", "v9", None))


def test_base_prompt_must_keep_placeholders() -> None:
    store = PromptStore(":memory:")
    for bad in ("", "Sin rutas", "Solo {{ENTRADA}}"):
        with pytest.raises(PromptError):
            run(store.save("base", bad, "", None))
    run(store.save("extra", "", "vaciar", None))  # extra may be empty
    with pytest.raises(PromptError):
        run(store.save("otro", "x", "", None))


def test_render_adds_extra_instructions() -> None:
    text = render(BASE_V1, "Responde solo sobre SSIS.", "az://a/in/", "az://b/out")
    assert text.startswith("Eres un experto. Entrada az://a/in, salida az://b/out.")
    assert text.rstrip().endswith("Instrucciones adicionales:\nResponde solo sobre SSIS.")
    assert "Instrucciones adicionales" not in render(BASE_V1, "  ", "az://a", "az://b")


def client(store, settings=SETTINGS, model=None) -> TestClient:
    return TestClient(create_app(settings, model=model or ScriptedModel(), mcp=FakeMcp(),
                                 storage=FakeStorage(), prompt_store=store))


def test_prompt_endpoints_and_chat_uses_active_prompt() -> None:
    store = PromptStore(":memory:")
    model = ScriptedModel({"content": "ok", "tool_calls": []})
    with client(store, model=model) as c:
        r = c.get("/api/prompts", params={"input": "az://ssis-packages/F", "output": "az://adf-artifacts/F"})
        body = r.json()
        assert body["can_edit"] is True
        assert body["prompts"]["base"]["active"] == "factory"
        assert '"az://ssis-packages/F/P.dtsx"' in body["effective"]

        assert c.post("/api/prompts/base", json={"text": "sin rutas"}).status_code == 400
        r = c.post("/api/prompts/base", json={"text": BASE_V1, "note": "corto"})
        assert r.status_code == 200 and r.json()["active"] == "v1"
        c.post("/api/prompts/extra", json={"text": "Responde solo sobre SSIS y ADF."})

        c.post("/api/chat", json={**SESSION, "messages": [{"role": "user", "content": "hola"}]})
        system = model.seen[0][0]["content"]
        assert "Entrada az://ssis-packages/Finanzas, salida az://adf-artifacts/Finanzas." in system
        assert "Responde solo sobre SSIS y ADF." in system

        r = c.post("/api/prompts/base/activate", json={"version": "factory"})
        assert r.json()["active_text"] == FACTORY_BASE
        assert c.post("/api/prompts/nada", json={"text": "x"}).status_code == 404


def test_only_admins_can_edit_when_configured() -> None:
    settings = replace(SETTINGS, admin_users=["Ana@Inetum.com"])
    with client(PromptStore(":memory:"), settings=settings) as c:
        luis = {"X-MS-CLIENT-PRINCIPAL-NAME": "luis@inetum.com"}
        ana = {"X-MS-CLIENT-PRINCIPAL-NAME": "ana@inetum.com"}
        assert c.get("/api/prompts", headers=luis).json()["can_edit"] is False
        assert c.post("/api/prompts/extra", json={"text": "x"}, headers=luis).status_code == 403
        assert c.post("/api/prompts/base/activate", json={"version": "factory"},
                      headers=luis).status_code == 403
        assert c.post("/api/prompts/extra", json={"text": "x"}, headers=ana).status_code == 200


def test_chat_falls_back_to_factory_if_store_fails() -> None:
    class Broken(PromptStore):
        async def active_texts(self):
            raise RuntimeError("base de datos bloqueada")

    model = ScriptedModel({"content": "ok", "tool_calls": []})
    with client(Broken(":memory:"), model=model) as c:
        r = c.post("/api/chat", json={**SESSION, "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    assert model.seen[0][0]["content"].startswith("Eres un asistente experto")


def test_sqlite_file_survives_restart_and_concurrent_saves(tmp_path) -> None:
    path = str(tmp_path / "sub" / "prompts.db")  # folder is created

    async def scenario():
        store = PromptStore(path)
        await asyncio.gather(*(store.save("extra", f"texto {i}", "", "a") for i in range(10)))
        await store.activate("extra", "v3", "b")
        store.close()

        again = PromptStore(path)  # e.g. after an App Service restart
        s = await again.state("extra")
        again.close()
        return s

    s = run(scenario())
    assert [v["number"] for v in s["versions"]] == list(range(10, 0, -1))
    assert s["active"] == "v3" and s["activated_by"] == "b"


def test_old_versions_are_pruned(monkeypatch) -> None:
    import app.prompts as prompts

    monkeypatch.setattr(prompts, "MAX_VERSIONS", 3)
    store = PromptStore(":memory:")
    for i in range(5):
        s = run(store.save("extra", f"t{i}", "", None))
    assert [v["id"] for v in s["versions"]] == ["v5", "v4", "v3"]
    with pytest.raises(PromptError):
        run(store.activate("extra", "v1", None))
