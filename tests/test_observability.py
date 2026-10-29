"""El worker debe dejar un span por ejecución con el job_id como sesión, y marcarlo con error si falla."""
import json

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from app import config
from app.observability import tracing
from app.queue import jobs, worker
from tests.test_worker import GrafoFalso


@pytest.fixture
def spans(monkeypatch):
    exportador = InMemorySpanExporter()
    proveedor = TracerProvider()
    proveedor.add_span_processor(SimpleSpanProcessor(exportador))
    monkeypatch.setattr(worker, "tracer", proveedor.get_tracer("test"))
    return exportador


async def _mensaje(r) -> tuple[str, dict]:
    job_id = await jobs.crear_job(r, "¿Puedo tener mascota?")
    _, crudo = await r.brpop([jobs.COLA], timeout=1)
    return job_id, json.loads(crudo)


async def test_span_por_job_con_sesion_y_entrada_salida(r, spans):
    job_id, mensaje = await _mensaje(r)

    await worker.procesar_mensaje(r, GrafoFalso(respuesta="Sí, con condiciones"), mensaje)

    [span] = spans.get_finished_spans()
    assert span.name == "job"
    assert span.attributes["session.id"] == job_id
    assert span.attributes["openinference.span.kind"] == "CHAIN"
    assert span.attributes["job.tipo"] == "start"
    assert span.attributes["input.value"] == "¿Puedo tener mascota?"
    assert span.attributes["output.value"] == "Sí, con condiciones"
    assert span.attributes["job.status"] == "DONE"
    assert span.status.status_code == StatusCode.OK


async def test_span_de_job_fallido_queda_en_error(r, spans):
    _, mensaje = await _mensaje(r)

    await worker.procesar_mensaje(r, GrafoFalso(error=RuntimeError("boom")), mensaje)

    [span] = spans.get_finished_spans()
    assert span.attributes["job.status"] == "FAILED"
    assert span.status.status_code == StatusCode.ERROR
    assert any(evento.name == "exception" for evento in span.events)


def test_sin_endpoint_no_configura_trazas(monkeypatch):
    monkeypatch.setattr(config.settings, "phoenix_collector_endpoint", "")
    assert tracing.configurar_trazas() is None
