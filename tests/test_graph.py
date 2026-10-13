"""Diagram model built from ADF pipeline JSON."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.graph import build_pipeline_graph, describe_graph, load_pipeline

DATA = Path(__file__).parent / "data"


def graph(name: str) -> dict:
    return build_pipeline_graph(load_pipeline((DATA / name).read_bytes()))


def test_sequential_pipeline_from_converter() -> None:
    g = graph("PL_CargaClientes.json")
    labels = [n["label"] for n in g["nodes"]]
    assert labels == [
        "Obtener Fecha Corte", "Limpiar Stage", "Cargar Clientes", "Registrar Error"
    ]
    assert [(e["source"], e["target"], e["condition"]) for e in g["edges"]] == [
        ("Obtener Fecha Corte", "Limpiar Stage", "Succeeded"),
        ("Limpiar Stage", "Cargar Clientes", "Succeeded"),
        ("Cargar Clientes", "Registrar Error", "Failed"),
    ]
    by = {n["label"]: n for n in g["nodes"]}
    assert by["Registrar Error"]["warning"] is True
    assert by["Cargar Clientes"]["category"] == "dataflow"
    sql = {"label": "SQL", "value": "TRUNCATE TABLE stg.Clientes"}
    assert sql in by["Limpiar Stage"]["details"]
    assert g["stats"] == {"activities": 4, "warnings": 1, "containers": 0}


def test_containers_branches_and_scoped_dependencies() -> None:
    g = graph("PL_Complejo.json")
    by = {n["id"]: n for n in g["nodes"]}
    # ForEach holds its inner activities; inner dependency is scoped to the loop
    assert by["Por Cada Archivo/Copiar Archivo"]["parent"] == "Por Cada Archivo"
    inner = ("Por Cada Archivo/Copiar Archivo", "Por Cada Archivo/Marcar Procesado", "Succeeded")
    assert inner in {(e["source"], e["target"], e["condition"]) for e in g["edges"]}
    # IfCondition gets one compound per branch
    assert by["Hay Errores/Verdadero"]["branch"]
    assert by["Hay Errores/Falso"]["parent"] == "Hay Errores"
    assert by["Hay Errores/Verdadero/Notificar"]["warning"] is True
    # multiple conditions on one dependency → one edge each
    conds = sorted(e["condition"] for e in g["edges"] if e["target"] == "Registrar Fin")
    assert conds == ["Failed", "Succeeded"]
    assert g["stats"] == {"activities": 8, "warnings": 1, "containers": 2}
    assert g["parameters"] == ["FechaCorte"]
    # container JSON omits the nested activities (they are separate nodes)
    assert "typeProperties" not in by["Por Cada Archivo"]["json"]
    # every edge points to existing nodes
    assert all(e["source"] in by and e["target"] in by for e in g["edges"])


def test_describe_graph_for_the_model() -> None:
    text = describe_graph(graph("PL_Complejo.json"))
    assert "Copiar Archivo · Copy (dentro de Por Cada Archivo)" in text
    assert "Notificar · WebActivity (dentro de Verdadero) [REVISIÓN MANUAL]" in text
    assert "Hay Errores --Failed--> Registrar Fin" in text


@pytest.mark.parametrize("payload", [b"[]", b'{"name": "x"}', b'{"properties": {}}'])
def test_rejects_non_pipelines(payload: bytes) -> None:
    with pytest.raises(ValueError):
        load_pipeline(payload)


def test_graph_is_json_serializable() -> None:
    json.dumps(graph("PL_Complejo.json"))
