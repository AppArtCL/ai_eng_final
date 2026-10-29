"""API REST del orquestador multi-agente.

Los endpoints nunca ejecutan el grafo: solo leen y escriben en Redis (redis.asyncio) y responden
de inmediato. El trabajo pesado lo hacen los workers que el lifespan lanza en el mismo event loop.

Uso:
    uv run uvicorn app.api.main:app
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app import config
from app.api.schemas import DecisionHumana, EstadoTarea, NuevaTarea, TareaEncolada
from app.graph.builder import checkpointer_redis, construir_grafo
from app.observability.tracing import cerrar_trazas, configurar_trazas
from app.queue import jobs
from app.queue.worker import bucle_worker

logger = logging.getLogger(__name__)


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
            await app.state.redis.aclose()
            cerrar_trazas(proveedor_trazas)


def _redis(request: Request) -> Redis:
    return request.app.state.redis


def crear_app() -> FastAPI:
    app = FastAPI(
        title="Orquestador Edificio Riesco 6540",
        description="Preguntas sobre el reglamento resueltas por un equipo de agentes, con aprobación humana para acciones.",
        lifespan=lifespan,
        # Solo se trazan las ejecuciones del agente (worker), no cada petición HTTP del polling.
        telemetry={"tracing": False},
    )

    @app.exception_handler(RedisConnectionError)
    async def redis_no_disponible(request: Request, exc: RedisConnectionError) -> JSONResponse:
        logger.error("Redis no disponible: %s", exc)
        return JSONResponse(status_code=503, content={"detail": "Redis no disponible"})

    @app.post("/tasks", status_code=202, response_model=TareaEncolada)
    async def crear_tarea(tarea: NuevaTarea, request: Request) -> TareaEncolada:
        """Encola la pregunta y devuelve el job_id sin esperar al LLM."""
        job_id = await jobs.crear_job(_redis(request), tarea.pregunta)
        return TareaEncolada(job_id=job_id, status="PENDING")

    @app.get("/tasks/{job_id}", response_model=EstadoTarea)
    async def estado_tarea(job_id: str, request: Request) -> EstadoTarea:
        job = await jobs.obtener_job(_redis(request), job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Tarea no encontrada")
        return EstadoTarea(**job)

    @app.post("/tasks/{job_id}/approve", status_code=202, response_model=TareaEncolada)
    async def aprobar_tarea(job_id: str, decision: DecisionHumana, request: Request) -> TareaEncolada:
        """Registra la decisión humana y encola la reanudación del grafo (aprobada o rechazada)."""
        r = _redis(request)
        if await jobs.solicitar_reanudacion(r, job_id, decision.aprobado, decision.comentario):
            return TareaEncolada(job_id=job_id, status="PENDING")
        job = await jobs.obtener_job(r, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Tarea no encontrada")
        raise HTTPException(status_code=409, detail=f"La tarea no espera aprobación (estado actual: {job['status']})")

    @app.get("/health")
    async def salud(request: Request) -> dict[str, Literal["ok"]]:
        await _redis(request).ping()
        return {"redis": "ok"}

    return app


app = crear_app()
