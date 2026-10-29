"""API REST del orquestador multi-agente.

Los endpoints nunca ejecutan el grafo: solo leen y escriben en Redis (redis.asyncio) y responden
de inmediato. El trabajo pesado lo hacen los workers que el lifespan lanza en el mismo event loop.
El progreso de un job se sigue por polling (GET /tasks/{id}) o en vivo por SSE (GET /tasks/{id}/events).

Uso:
    uv run uvicorn app.api.main:app
"""
import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app import config
from app.agents import tools
from app.api.schemas import (
    DecisionHumana,
    ErrorRespuesta,
    EstadoTarea,
    JobId,
    NuevaTarea,
    Salud,
    TareaEncolada,
)
from app.graph.builder import checkpointer_redis, construir_grafo
from app.observability.tracing import cerrar_trazas, configurar_trazas
from app.queue import events, jobs
from app.queue.worker import bucle_worker

logger = logging.getLogger(__name__)

ESPERA_EVENTOS_MS = 5000  # XREAD bloquea a lo más esto; luego revisa si el cliente sigue conectado


def configurar_logging() -> None:
    """Configura el logging de la aplicación (solo aquí, en el punto de entrada)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    for nombre in ("httpx", "httpx2"):
        logging.getLogger(nombre).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Activa las trazas, abre Redis y el checkpointer, compila el grafo y lanza los workers."""
    configurar_logging()
    proveedor_trazas = configurar_trazas()
    app.state.redis = Redis.from_url(config.settings.redis_url, decode_responses=True)
    tools.configurar_redis(app.state.redis)  # la herramienta del gestor usa el mismo pool
    # Construir el RAG (leer documentos, armar BM25) es CPU síncrono: se hace en un thread, una vez,
    # antes de aceptar trabajo; así la primera búsqueda no bloquea el event loop.
    rag = await asyncio.to_thread(tools.get_rag)
    await rag.aabrir()  # una sesión HTTP con Pinecone para todas las búsquedas concurrentes
    async with checkpointer_redis(config.settings.redis_url) as saver:
        grafo = construir_grafo(saver)
        # Antes de lanzar los workers: lo que quedó a medias en un apagado anterior vuelve a la cola.
        if recuperados := await jobs.recuperar_huerfanos(app.state.redis):
            logger.warning("Se recuperaron %d jobs interrumpidos: %s", len(recuperados), recuperados)
        workers = [
            asyncio.create_task(bucle_worker(app.state.redis, grafo, f"worker-{i}"))
            for i in range(1, config.settings.num_workers + 1)
        ]
        logger.info("API lista con %d workers.", len(workers))
        try:
            yield
        finally:
            for tarea in workers:
                tarea.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            await rag.acerrar()
            await app.state.redis.aclose()
            cerrar_trazas(proveedor_trazas)


def _redis(request: Request) -> Redis:
    return request.app.state.redis


async def _job_existente(job_id: JobId, request: Request) -> str:
    """Dependencia: 404 si el job no existe. Se resuelve antes de que empiece la respuesta,
    algo necesario en el endpoint SSE: una vez abierto el stream ya no se puede cambiar el status."""
    if await jobs.obtener_job(_redis(request), job_id) is None:
        raise HTTPException(status_code=404, detail="Tarea no encontrada")
    return job_id


def crear_app() -> FastAPI:
    app = FastAPI(
        title="Orquestador Edificio Riesco 6540",
        description="Preguntas sobre el reglamento resueltas por un equipo de agentes, con aprobación humana para acciones.",
        lifespan=lifespan,
        # Solo se trazan las ejecuciones del agente (worker), no cada petición HTTP del polling.
        telemetry={"tracing": False},
        # Todos los endpoints dependen de Redis: si se cae, responden 503 (ver redis_no_disponible).
        responses={503: {"model": ErrorRespuesta, "description": "Redis no disponible"}},
        openapi_tags=[
            {"name": "tareas", "description": "Preguntas al equipo de agentes y aprobación humana de acciones"},
            {"name": "sistema", "description": "Estado del servicio"},
        ],
    )

    @app.exception_handler(RedisConnectionError)
    async def redis_no_disponible(request: Request, exc: RedisConnectionError) -> JSONResponse:
        logger.error("Redis no disponible: %s", exc)
        return JSONResponse(status_code=503, content={"detail": "Redis no disponible"})

    @app.post("/tasks", status_code=202, response_model=TareaEncolada, tags=["tareas"],
              summary="Encolar una pregunta o solicitud")
    async def crear_tarea(tarea: NuevaTarea, request: Request) -> TareaEncolada:
        """Encola la pregunta y devuelve el job_id sin esperar al LLM."""
        job_id = await jobs.crear_job(_redis(request), tarea.pregunta)
        return TareaEncolada(job_id=job_id, status="PENDING")

    @app.get("/tasks/{job_id}", response_model=EstadoTarea, tags=["tareas"],
             summary="Consultar el estado de un trabajo (polling)",
             responses={404: {"model": ErrorRespuesta, "description": "Tarea no encontrada"}})
    async def estado_tarea(job_id: JobId, request: Request) -> EstadoTarea:
        """Estado actual: PENDING, RUNNING, WAITING_APPROVAL, DONE o FAILED, con la respuesta o el error."""
        job = await jobs.obtener_job(_redis(request), job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Tarea no encontrada")
        return EstadoTarea(**job)

    @app.post("/tasks/{job_id}/approve", status_code=202, response_model=TareaEncolada, tags=["tareas"],
              summary="Aprobar o rechazar una acción pendiente",
              responses={404: {"model": ErrorRespuesta, "description": "Tarea no encontrada"},
                         409: {"model": ErrorRespuesta, "description": "La tarea no espera aprobación"}})
    async def aprobar_tarea(job_id: JobId, decision: DecisionHumana, request: Request) -> TareaEncolada:
        """Registra la decisión humana y encola la reanudación del grafo (aprobada o rechazada)."""
        r = _redis(request)
        if await jobs.solicitar_reanudacion(r, job_id, decision.aprobado, decision.comentario):
            return TareaEncolada(job_id=job_id, status="PENDING")
        job = await jobs.obtener_job(r, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Tarea no encontrada")
        raise HTTPException(status_code=409, detail=f"La tarea no espera aprobación (estado actual: {job['status']})")

    @app.get("/tasks/{job_id}/events", response_class=EventSourceResponse, tags=["tareas"],
             summary="Seguir el progreso en vivo (Server-Sent Events)",
             responses={404: {"model": ErrorRespuesta, "description": "Tarea no encontrada"}})
    async def eventos_tarea(
        job_id: Annotated[str, Depends(_job_existente)],
        request: Request,
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> AsyncIterator[ServerSentEvent]:
        """Progreso del job en vivo (Server-Sent Events): estado, nodos terminados y tokens de la respuesta.

        El stream se cierra cuando el job llega a DONE, FAILED o WAITING_APPROVAL. Tras aprobar,
        el cliente vuelve a conectarse; con el header Last-Event-ID retoma desde el último evento.
        """
        r = _redis(request)
        desde = last_event_id or "0"
        while not await request.is_disconnected():
            for id_, tipo, datos in await events.leer(r, job_id, desde, bloqueo_ms=ESPERA_EVENTOS_MS):
                desde = id_
                yield ServerSentEvent(event=tipo, data=datos, id=id_)
                if events.es_final(tipo, datos):
                    return

    @app.get("/health", response_model=Salud, tags=["sistema"], summary="Verificar que la API llega a Redis")
    async def salud(request: Request) -> Salud:
        await _redis(request).ping()
        return Salud(redis="ok")

    return app


app = crear_app()
