"""Worker en segundo plano: consume la cola de Redis y ejecuta el grafo.

Corre como coroutines dentro del event loop de la API (lanzadas en el lifespan de FastAPI).
Todos los nodos del grafo son async (ainvoke de punta a punta), así que mientras un job espera
al LLM, a Pinecone o a Redis, el mismo event loop atiende la API y los demás workers.

El grafo se ejecuta con astream en vez de ainvoke: produce el mismo resultado, pero deja ver el
progreso (qué nodo terminó y los tokens de la respuesta final), que se publica en app.queue.events
para el endpoint SSE.
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

from app import config
from app.graph.state import estado_inicial
from app.observability.tracing import obtener_tracer
from app.queue import events, jobs

logger = logging.getLogger(__name__)
tracer = obtener_tracer()

ESPERA_COLA_SEG = 1      # timeout de BLMOVE: permite cancelar el worker sin esperar un mensaje
ESPERA_REINTENTO_SEG = 2  # pausa tras perder la conexión con Redis, para no girar en vacío
NODO_RESPUESTA = "sintesis"  # solo se transmiten token a token los mensajes de este nodo


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


async def _cambiar_estado(r: Redis, job_id: str, status: jobs.Estado, **campos) -> None:
    """Actualiza el job y publica el cambio: el polling y el stream SSE ven siempre lo mismo."""
    await jobs.actualizar_job(r, job_id, status=status, **campos)
    await events.publicar(r, job_id, "estado", status=status, **campos)


async def ejecutar_grafo(r: Redis, grafo, entrada: Any, config_grafo: dict, job_id: str) -> None:
    """Corre el grafo con astream y publica su progreso como eventos.

    stream_mode="updates" entrega un dict {nodo: cambios} cada vez que un nodo termina.
    stream_mode="messages" entrega los tokens de cada llamada al LLM a medida que llegan; solo se
    publican los de la síntesis (los de los agentes internos son pasos intermedios). Esos tokens
    son un borrador: si la validación de cifras pide reescribir, la respuesta definitiva es la del
    evento de estado DONE.
    """
    async for modo, dato in grafo.astream(entrada, config_grafo, stream_mode=["updates", "messages"]):
        if modo == "updates":
            for nodo in dato:
                if nodo != "__interrupt__":
                    await events.publicar(r, job_id, "nodo", nodo=nodo)
        elif modo == "messages":
            fragmento, metadata = dato
            if metadata.get("langgraph_node") == NODO_RESPUESTA and fragmento.content:
                await events.publicar(r, job_id, "token", texto=fragmento.content)


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

    config_grafo = {"configurable": {"thread_id": job_id}}
    atributos = {
        SpanAttributes.OPENINFERENCE_SPAN_KIND: OpenInferenceSpanKindValues.CHAIN.value,
        SpanAttributes.SESSION_ID: job_id,
        SpanAttributes.INPUT_VALUE: job["pregunta"],
        "job.id": job_id,
        "job.tipo": mensaje["tipo"],
    }
    with using_session(job_id), tracer.start_as_current_span("job", attributes=atributos) as span:
        try:
            await _cambiar_estado(r, job_id, "RUNNING")
            invocar, entrada = elegir_entrada(await grafo.aget_state(config_grafo), mensaje, job["pregunta"])
            if not invocar:
                logger.info("Job %s: su hilo ya había terminado; solo se registra el resultado.", job_id)
            elif entrada is None:
                span.set_attribute("job.retomado", True)
                logger.info("Job %s: se retoma desde su último checkpoint.", job_id)

            if invocar:
                # Tope de latencia (M1): un LLM o una búsqueda colgada no deja el job en RUNNING para siempre.
                async with asyncio.timeout(config.settings.job_timeout_seg):
                    await ejecutar_grafo(r, grafo, entrada, config_grafo, job_id)
            snapshot = await grafo.aget_state(config_grafo)

            if snapshot.interrupts:
                await _cambiar_estado(r, job_id, "WAITING_APPROVAL",
                                      aprobacion_pendiente=snapshot.interrupts[0].value)
                span.set_attribute("job.status", "WAITING_APPROVAL")
                logger.info("Job %s en espera de aprobación humana.", job_id)
            else:
                respuesta = str(snapshot.values["messages"][-1].content)
                await _cambiar_estado(r, job_id, "DONE", respuesta=respuesta)
                span.set_attribute("job.status", "DONE")
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, respuesta)
                logger.info("Job %s terminado.", job_id)
            span.set_status(Status(StatusCode.OK))
        except Exception as e:
            # Sin esto el cliente quedaría haciendo polling para siempre sobre un job RUNNING.
            logger.exception("Job %s falló.", job_id)
            if isinstance(e, TimeoutError):
                error = f"TimeoutError: la ejecución superó {config.settings.job_timeout_seg} s"
            else:
                error = f"{type(e).__name__}: {e}"
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, error))
            span.set_attribute("job.status", "FAILED")
            try:
                await _cambiar_estado(r, job_id, "FAILED", error=error)
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
