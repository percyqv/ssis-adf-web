"""
Build a diagram model (nodes + edges) from an ADF pipeline JSON.

The result is deterministic (no AI involved) and is rendered by the web UI with
Cytoscape.js. Container activities (ForEach, Until, IfCondition, Switch) become
compound nodes that hold their inner activities.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Activity type → visual category (drives icon and colour in the UI)
_CATEGORY = {
    "Copy": "move",
    "ExecuteDataFlow": "dataflow",
    "Script": "sql",
    "Lookup": "sql",
    "SqlServerStoredProcedure": "sql",
    "GetMetadata": "sql",
    "ForEach": "loop",
    "Until": "loop",
    "IfCondition": "branch",
    "Switch": "branch",
    "ExecutePipeline": "pipeline",
    "WebActivity": "web",
    "WebHook": "web",
    "AzureFunctionActivity": "function",
    "Custom": "function",
    "SetVariable": "variable",
    "AppendVariable": "variable",
    "Filter": "variable",
    "Wait": "wait",
    "Fail": "fail",
    "Delete": "move",
}

_TYPE_LABEL = {
    "Copy": "Copiar datos",
    "ExecuteDataFlow": "Data Flow",
    "Script": "Script SQL",
    "Lookup": "Búsqueda",
    "SqlServerStoredProcedure": "Procedimiento almacenado",
    "GetMetadata": "Obtener metadatos",
    "ForEach": "ForEach",
    "Until": "Until",
    "IfCondition": "If",
    "Switch": "Switch",
    "ExecutePipeline": "Ejecutar pipeline",
    "WebActivity": "Web",
    "AzureFunctionActivity": "Azure Function",
    "SetVariable": "Asignar variable",
    "AppendVariable": "Añadir a variable",
    "Wait": "Esperar",
    "Fail": "Error",
    "Delete": "Eliminar",
}

_REVIEW = re.compile(r"\[(MANUAL REVIEW|UNSUPPORTED)[^\]]*\]", re.IGNORECASE)


def _ref(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("referenceName")
    return None


def _expr(value: Any) -> str:
    if isinstance(value, dict) and "value" in value:
        return str(value["value"])
    return "" if value is None else str(value)


def _details(act: dict[str, Any]) -> list[dict[str, str]]:
    """Human-readable key facts for the details panel."""
    t = act.get("type", "")
    tp = act.get("typeProperties", {}) or {}
    rows: list[tuple[str, str]] = []

    ls = _ref(act.get("linkedServiceName"))
    if t == "Copy":
        src, sink = tp.get("source", {}) or {}, tp.get("sink", {}) or {}
        ins = [_ref(i) for i in act.get("inputs", []) or []]
        outs = [_ref(o) for o in act.get("outputs", []) or []]
        rows += [
            ("Origen", f"{src.get('type', '?')} · {', '.join(filter(None, ins)) or '—'}"),
            ("Destino", f"{sink.get('type', '?')} · {', '.join(filter(None, outs)) or '—'}"),
        ]
        if src.get("sqlReaderQuery"):
            rows.append(("Consulta", _expr(src["sqlReaderQuery"])))
        if sink.get("writeBehavior"):
            rows.append(("Escritura", str(sink["writeBehavior"])))
    elif t == "Script":
        for i, script in enumerate(tp.get("scripts", []) or [], 1):
            rows.append((f"SQL {i}" if len(tp.get("scripts", [])) > 1 else "SQL",
                         _expr(script.get("text"))))
    elif t == "Lookup":
        src = tp.get("source", {}) or {}
        rows += [("Dataset", _ref(tp.get("dataset")) or "—"),
                 ("Consulta", _expr(src.get("sqlReaderQuery")) or "—"),
                 ("Solo primera fila", "sí" if tp.get("firstRowOnly") else "no")]
    elif t == "SqlServerStoredProcedure":
        rows.append(("Procedimiento", str(tp.get("storedProcedureName", "—"))))
        params = tp.get("storedProcedureParameters") or {}
        if params:
            rows.append(("Parámetros", ", ".join(params)))
    elif t == "ExecuteDataFlow":
        rows.append(("Data flow", _ref(tp.get("dataflow")) or "—"))
        compute = tp.get("compute") or {}
        if compute:
            rows.append(("Cómputo", f"{compute.get('computeType', '')} · "
                                    f"{compute.get('coreCount', '')} núcleos"))
    elif t == "ExecutePipeline":
        rows.append(("Pipeline", _ref(tp.get("pipeline")) or "—"))
    elif t in ("WebActivity", "WebHook", "AzureFunctionActivity"):
        rows += [("Método", str(tp.get("method", "—"))),
                 ("URL", _expr(tp.get("url") or tp.get("functionName")))]
    elif t == "ForEach":
        rows += [("Elementos", _expr(tp.get("items"))),
                 ("Secuencial", "sí" if tp.get("isSequential") else "no")]
    elif t == "Until":
        rows.append(("Condición", _expr(tp.get("expression"))))
    elif t == "IfCondition":
        rows.append(("Condición", _expr(tp.get("expression"))))
    elif t == "Switch":
        rows.append(("Valor", _expr(tp.get("on"))))
    elif t == "Wait":
        rows.append(("Segundos", str(tp.get("waitTimeInSeconds", "—"))))
    elif t in ("SetVariable", "AppendVariable"):
        rows += [("Variable", str(tp.get("variableName", "—"))),
                 ("Valor", _expr(tp.get("value")))]

    if ls:
        rows.insert(0, ("Linked service", ls))
    policy = act.get("policy") or {}
    if policy.get("retry"):
        every = policy.get("retryIntervalInSeconds", "?")
        rows.append(("Reintentos", f"{policy['retry']} (cada {every} s)"))
    if policy.get("timeout"):
        rows.append(("Tiempo máximo", str(policy["timeout"])))
    return [{"label": k, "value": v} for k, v in rows if v not in (None, "")]


def _children(act: dict[str, Any]) -> list[tuple[str | None, list[dict[str, Any]]]]:
    """Inner activity lists of a container: [(branch_label, activities), ...]."""
    tp = act.get("typeProperties", {}) or {}
    t = act.get("type")
    if t in ("ForEach", "Until"):
        return [(None, tp.get("activities", []) or [])]
    if t == "IfCondition":
        return [("Verdadero", tp.get("ifTrueActivities", []) or []),
                ("Falso", tp.get("ifFalseActivities", []) or [])]
    if t == "Switch":
        branches = [(f"Caso {c.get('value')}", c.get("activities", []) or [])
                    for c in tp.get("cases", []) or []]
        branches.append(("Por defecto", tp.get("defaultActivities", []) or []))
        return branches
    return []


def build_pipeline_graph(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Return {"name", "nodes", "edges", "stats"} for an ADF pipeline JSON."""
    props = pipeline.get("properties", {}) or {}
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    def add_scope(activities: list[dict[str, Any]], parent: str | None, prefix: str) -> None:
        ids = {a.get("name"): f"{prefix}{a.get('name')}" for a in activities if a.get("name")}
        for act in activities:
            name = act.get("name") or "?"
            node_id = ids.get(name, f"{prefix}{name}")
            t = act.get("type", "Desconocido")
            desc = act.get("description") or ""
            warnings = [m.group(0) for m in _REVIEW.finditer(desc)]
            branches = _children(act)
            nodes.append({
                "id": node_id,
                "parent": parent,
                "label": name,
                "type": t,
                "typeLabel": _TYPE_LABEL.get(t, t),
                "category": _CATEGORY.get(t, "other"),
                "container": bool(branches),
                "warning": bool(warnings),
                "description": desc,
                "details": _details(act),
                "json": {k: v for k, v in act.items()
                         if k not in ("typeProperties",) or not branches},
            })
            for dep in act.get("dependsOn", []) or []:
                src = ids.get(dep.get("activity"))
                if src:
                    for cond in dep.get("dependencyConditions", ["Succeeded"]) or ["Succeeded"]:
                        edges.append({
                            "id": f"{src}->{node_id}:{cond}",
                            "source": src, "target": node_id, "condition": cond,
                        })
            if len(branches) == 1:
                add_scope(branches[0][1], node_id, f"{node_id}/")
            else:
                for label, inner in branches:
                    branch_id = f"{node_id}/{label}"
                    nodes.append({
                        "id": branch_id, "parent": node_id, "label": label,
                        "type": "Branch", "typeLabel": "Rama", "category": "branch",
                        "container": True, "warning": False, "branch": True,
                        "description": "", "details": [], "json": {},
                    })
                    add_scope(inner, branch_id, f"{branch_id}/")

    add_scope(props.get("activities", []) or [], None, "")

    real = [n for n in nodes if not n.get("branch")]
    return {
        "name": pipeline.get("name", ""),
        "description": props.get("description", ""),
        "parameters": list((props.get("parameters") or {}).keys()),
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "activities": len(real),
            "warnings": sum(1 for n in real if n["warning"]),
            "containers": sum(1 for n in real if n["container"]),
        },
    }


def describe_graph(graph: dict[str, Any]) -> str:
    """Compact text version of the graph for the language model."""
    by_id = {n["id"]: n for n in graph["nodes"]}
    lines = [f"Pipeline {graph['name']}: {graph['stats']['activities']} actividades, "
             f"{graph['stats']['warnings']} con revisión manual."]
    for n in graph["nodes"]:
        if n.get("branch"):
            continue
        where = f" (dentro de {by_id[n['parent']]['label']})" if n.get("parent") else ""
        warn = " [REVISIÓN MANUAL]" if n["warning"] else ""
        lines.append(f"- {n['label']} · {n['type']}{where}{warn}")
    for e in graph["edges"]:
        lines.append(f"  {by_id[e['source']]['label']} --{e['condition']}--> "
                     f"{by_id[e['target']]['label']}")
    return "\n".join(lines)


def load_pipeline(data: bytes) -> dict[str, Any]:
    pipeline = json.loads(data.decode("utf-8-sig"))
    if not isinstance(pipeline, dict) or "properties" not in pipeline:
        raise ValueError("El archivo no es un pipeline de ADF")
    if "activities" not in (pipeline.get("properties") or {}):
        raise ValueError("El archivo no es un pipeline de ADF (no tiene actividades)")
    return pipeline
