"""Tests de la API sin workers ni LLM: httpx con ASGITransport no ejecuta el lifespan,
así que cada test inyecta su propio cliente Redis (fakeredis) en app.state."""
import fakeredis
import pytest
from httpx import ASGITransport, AsyncClient

from app import jobs
from app.main import crear_app


@pytest.fixture
async def cliente(r):
    app = crear_app()
    app.state.redis = r
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _job_esperando(r) -> str:
    job_id = await jobs.crear_job(r, "Registra mi mudanza")
    await jobs.actualizar_job(r, job_id, status="WAITING_APPROVAL", aprobacion_pendiente={"accion": "mudanza"})
    return job_id


# --- POST /tasks -----------------------------------------------------------------

async def test_post_tasks_encola_y_responde_202_sin_procesar(cliente, r):
    resp = await cliente.post("/tasks", json={"pregunta": "¿Puedo tener mascota?"})

    assert resp.status_code == 202
    cuerpo = resp.json()
    assert cuerpo["status"] == "PENDING"
    # Sin workers, el job sigue PENDING: el endpoint solo encola, no ejecuta el grafo.
    job = await jobs.obtener_job(r, cuerpo["job_id"])
    assert job["status"] == "PENDING" and job["pregunta"] == "¿Puedo tener mascota?"
    assert await r.llen(jobs.COLA) == 1


@pytest.mark.parametrize("pregunta", ["", "x" * 2001])
async def test_post_tasks_valida_largo_de_pregunta(cliente, r, pregunta):
    resp = await cliente.post("/tasks", json={"pregunta": pregunta})

    assert resp.status_code == 422
    assert await r.llen(jobs.COLA) == 0


# --- GET /tasks/{id} ---------------------------------------------------------------

async def test_get_task_devuelve_estado(cliente, r):
    job_id = await _job_esperando(r)

    resp = await cliente.get(f"/tasks/{job_id}")

    assert resp.status_code == 200
    cuerpo = resp.json()
    assert cuerpo["job_id"] == job_id
    assert cuerpo["status"] == "WAITING_APPROVAL"
    assert cuerpo["aprobacion_pendiente"] == {"accion": "mudanza"}
    assert cuerpo["respuesta"] is None and cuerpo["error"] is None


async def test_get_task_inexistente_404(cliente):
    assert (await cliente.get("/tasks/no-existe")).status_code == 404


# --- POST /tasks/{id}/approve ----------------------------------------------------------

async def test_approve_encola_resume(cliente, r):
    job_id = await _job_esperando(r)
    await r.delete(jobs.COLA)

    resp = await cliente.post(f"/tasks/{job_id}/approve", json={"aprobado": True})

    assert resp.status_code == 202
    assert resp.json() == {"job_id": job_id, "status": "PENDING"}
    assert await r.llen(jobs.COLA) == 1


async def test_doble_approve_segundo_409(cliente, r):
    job_id = await _job_esperando(r)

    primero = await cliente.post(f"/tasks/{job_id}/approve", json={"aprobado": True})
    segundo = await cliente.post(f"/tasks/{job_id}/approve", json={"aprobado": False, "comentario": "no"})

    assert primero.status_code == 202
    assert segundo.status_code == 409
    assert "PENDING" in segundo.json()["detail"]


async def test_approve_job_no_esperando_409(cliente, r):
    job_id = await jobs.crear_job(r, "x")

    resp = await cliente.post(f"/tasks/{job_id}/approve", json={"aprobado": True})

    assert resp.status_code == 409


async def test_approve_inexistente_404(cliente):
    resp = await cliente.post("/tasks/no-existe/approve", json={"aprobado": True})
    assert resp.status_code == 404


async def test_approve_sin_campo_aprobado_422(cliente, r):
    job_id = await _job_esperando(r)
    assert (await cliente.post(f"/tasks/{job_id}/approve", json={})).status_code == 422


# --- Salud y Redis caído -------------------------------------------------------------------

async def test_health_ok(cliente):
    resp = await cliente.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"redis": "ok"}


@pytest.fixture
async def cliente_sin_redis():
    servidor = fakeredis.FakeServer()
    servidor.connected = False
    app = crear_app()
    app.state.redis = fakeredis.aioredis.FakeRedis(server=servidor, decode_responses=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_redis_caido_responde_503(cliente_sin_redis):
    assert (await cliente_sin_redis.post("/tasks", json={"pregunta": "hola"})).status_code == 503
    assert (await cliente_sin_redis.get("/tasks/abc")).status_code == 503
    assert (await cliente_sin_redis.get("/health")).status_code == 503


def test_fastapi_no_traza_peticiones_http():
    """FastAPI >=0.150 traza cada petición por defecto; el polling de GET /tasks inflaría
    el conteo de trazas y falsearía la latencia p95 de las ejecuciones en Phoenix."""
    assert crear_app()._telemetry["tracing"] is False
