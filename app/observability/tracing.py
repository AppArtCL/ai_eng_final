"""Trazas de cada ejecución hacia Arize Phoenix (OpenTelemetry + OpenInference).

LangChainInstrumentor crea un span por nodo del grafo, por llamada al LLM (con tokens, que Phoenix
convierte en costo) y por herramienta. El worker suma un span raíz "job" por ejecución con el
job_id como session.id, para agrupar el arranque y la reanudación tras el HITL.
"""
import logging

from openinference.instrumentation.langchain import LangChainInstrumentor
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from phoenix.otel import register

from app import config

logger = logging.getLogger(__name__)


def configurar_trazas() -> TracerProvider | None:
    """Registra el proveedor global de trazas e instrumenta LangChain/LangGraph."""
    if not config.settings.phoenix_collector_endpoint:
        logger.warning("PHOENIX_COLLECTOR_ENDPOINT vacío: las ejecuciones no se trazarán.")
        return None
    proveedor = register(
        endpoint=config.settings.phoenix_collector_endpoint,
        project_name=config.settings.phoenix_project,
        batch=True,  # exporta en segundo plano, sin frenar al worker
        verbose=False,
    )
    LangChainInstrumentor().instrument(tracer_provider=proveedor)
    logger.info("Trazas hacia %s (proyecto %s).", config.settings.phoenix_collector_endpoint, config.settings.phoenix_project)
    return proveedor


def cerrar_trazas(proveedor: TracerProvider | None) -> None:
    """Envía los spans pendientes antes de apagar la API."""
    if proveedor is not None:
        LangChainInstrumentor().uninstrument()
        proveedor.shutdown()


def obtener_tracer() -> trace.Tracer:
    return trace.get_tracer("app.queue.worker")
