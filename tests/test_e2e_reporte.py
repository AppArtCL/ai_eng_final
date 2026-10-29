"""El desglose por nodo de scripts/e2e.py suma los LLM que cuelgan de cada nodo, a cualquier profundidad."""
from scripts.e2e import desglosar


def _span(span_id, padre, nombre, tipo="chain", latencia=0.0, costo=None, tokens=None):
    resumen = {"total": {"cost": costo, "tokens": tokens}} if costo is not None else None
    return {"spanId": span_id, "parentId": padre, "name": nombre, "spanKind": tipo,
            "latencyMs": latencia, "costSummary": resumen}


def test_desglose_suma_llm_anidados_por_nodo():
    spans = [
        _span("job", None, "job"),
        _span("g", "job", "LangGraph"),
        _span("sup", "g", "supervisor", latencia=1000),
        _span("cadena", "sup", "RunnableSequence"),
        _span("llm1", "cadena", "ChatOpenAI", tipo="llm", costo=0.002, tokens=1200),
        _span("inv", "g", "investigador", latencia=4000),
        _span("agente", "inv", "LangGraph"),
        _span("llm2", "agente", "ChatOpenAI", tipo="llm", costo=0.001, tokens=500),
        _span("llm3", "agente", "ChatOpenAI", tipo="llm", costo=0.001, tokens=600),
        _span("sup2", "g", "supervisor", latencia=500),
        _span("llm4", "sup2", "ChatOpenAI", tipo="llm", costo=0.003, tokens=1300),
    ]

    por_nodo = desglosar(spans)

    assert por_nodo["supervisor"] == {"ejecuciones": 2, "latencia_ms": 1500, "tokens": 2500, "costo_usd": 0.005}
    assert por_nodo["investigador"]["tokens"] == 1100
    assert set(por_nodo) == {"supervisor", "investigador"}  # LangGraph, job, etc. no son nodos del grafo
