"""Worker en segundo plano: consume la cola de Redis y ejecuta el grafo.

Corre como coroutines dentro del event loop de la API (lanzadas en el lifespan de FastAPI).
El grafo se ejecuta con ainvoke: LangGraph corre los nodos síncronos del M6 en un threadpool,
así que las llamadas al LLM y a Pinecone no bloquean el event loop.
"""
import asyncio
import json
import logging
from typing import Any

from langgraph.types import Command
from openinference.instrumentation import using_session
from openinference.semconv.trace import OpenInferenceSpanKindValues, SpanAttributes
from opentelemetry.trace import Status, StatusCode
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app import jobs
from app.observability import obtener_tracer
from app.state import estado_inicial

logger = logging.getLogger(__name__)
tracer = obtener_tracer()

ESPERA_COLA_SEG = 1      # timeout de BLMOVE: permite cancelar el worker sin esperar un mensaje
ESPERA_REINTENTO_SEG = 2  # pausa tras perder la conexión con Redis, para no girar en vacío


def elegir_entrada(snapshot, mensaje: dict, pregunta: str) -> tuple[bool, Any]:
    """Decide con qué invocar el grafo según lo que ya quedó guardado en su checkpoint.

    Devuelve (invocar, entrada). Un mensaje puede llegar por segunda vez si la API se reinició
    a mitad del job (ver jobs.recuperar_huerfanos); el checkpoint dice en qué punto quedó:
    - Interrupción pendiente y mensaje "resume": se reanuda con la decisión humana.
    - Quedan nodos por ejecutar (snapshot.next): se continúa desde el último checkpoint con
      entrada None, sin repetir los nodos que ya terminaron ni duplicar la pregunta.
    - El hilo ya terminó (hay valores y nada pendiente): no se invoca; solo falta registrar el resultado.
    - Hilo nuevo: se parte con el estado inicial.
    """
    if mensaje["tipo"] == "resume" and snapshot.interrupts:
        return True, Command(resume=mensaje["resume"])
    if snapshot.next:
        return True, None
    if snapshot.values:
        return False, None
    return True, estado_inicial(pregunta)


async def procesar_mensaje(r: Redis, grafo, mensaje: dict) -> None:
    """Ejecuta (o retoma) un job y deja su estado final en Redis: DONE, WAITING_APPROVAL o FAILED.

    Todo corre dentro de un span "job" con session.id = job_id: los spans que LangChain crea
    adentro (nodos, LLM, herramientas) quedan como hijos y en la misma sesión de Phoenix.
    """
    job_id = mensaje["job_id"]
    job = await jobs.obtener_job(r, job_id)
    if job is None:
        logger.warning("Mensaje para un job inexistente (%s); se descarta.", job_id)
        return

    config = {"configurable": {"thread_id": job_id}}
    atributos = {
        SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
        SpanAttributes.SESSION_ID: job_id,
        SpanAttributes.INPUT_VALUE: job["pregunta"],
        "job.id": job_id,
        "job.tipo": mensaje["tipo"],
    }
    with using_session(job_id), tracer.start_as_current_span("job", attributes=atributos) as span:
        try:
            await jobs.actualizar_job(r, job_id, status="RUNNING")
            invocar, entrada = elegir_entrada(await grafo.aget_state(config), mensaje, job["pregunta"])
            if not invocar:
                logger.info("Job %s: su hilo ya había terminado; solo se registra el resultado.", job_id)
            elif entrada is None:
                span.set_attribute("job.retomado", True)
                logger.info("Job %s: se retoma desde su último checkpoint.", job_id)

            if invocar:
                await grafo.ainvoke(entrada, config)
            snapshot = await grafo.aget_state(config)

            if snapshot.interrupts:
                await jobs.actualizar_job(r, job_id, status="WAITING_APPROVAL",
                                          aprobacion_pendiente=snapshot.interrupts[0].value)
                span.set_attribute("job.status", "WAITING_APPROVAL")
                logger.info("Job %s en espera de aprobación humana.", job_id)
            else:
                respuesta = str(snapshot.values["messages"][-1].content)
                await jobs.actualizar_job(r, job_id, status="DONE", respuesta=respuesta)
                span.set_attribute("job.status", "DONE")
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, respuesta)
                logger.info("Job %s terminado.", job_id)
            span.set_status(Status(StatusCode.OK))
        except Exception as e:
            # Sin esto el cliente quedaría haciendo polling para siempre sobre un job RUNNING.
            logger.exception("Job %s falló.", job_id)
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, f"{type(e).__name__}: {e}"))
            span.set_attribute("job.status", "FAILED")
            try:
                await jobs.actualizar_job(r, job_id, status="FAILED", error=f"{type(e).__name__}: {e}")
            except Exception:
                logger.exception("No se pudo marcar el job %s como FAILED.", job_id)


async def bucle_worker(r: Redis, grafo, nombre: str) -> None:
    """Toma mensajes de la cola uno a uno hasta que la tarea se cancela.

    El mensaje se confirma (se borra de `cola:procesando`) solo cuando procesar_mensaje termina,
    incluso si el job quedó FAILED: reintentar un job que falla siempre lo dejaría en un ciclo.
    Si el worker se cancela a mitad (CancelledError al apagar la API), el mensaje NO se confirma
    y se recupera en el próximo arranque.
    """
    logger.info("Worker %s iniciado.", nombre)
    while True:
        try:
            crudo = await jobs.tomar_mensaje(r, ESPERA_COLA_SEG)
        except RedisConnectionError:
            logger.exception("Worker %s perdió la conexión con Redis; reintenta en %ss.", nombre, ESPERA_REINTENTO_SEG)
            await asyncio.sleep(ESPERA_REINTENTO_SEG)
            continue
        if crudo is None:
            continue

        try:
            mensaje = json.loads(crudo)
        except json.JSONDecodeError:
            logger.error("Worker %s descartó un mensaje que no es JSON: %r", nombre, crudo)
        else:
            await procesar_mensaje(r, grafo, mensaje)
        await jobs.confirmar_mensaje(r, crudo)
