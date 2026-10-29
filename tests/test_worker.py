import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.queue import jobs, worker
from app.graph.builder import checkpointer_redis, construir_grafo


HILO_VACIO = SimpleNamespace(interrupts=[], next=(), values={})


class GrafoFalso:
    """Imita la interfaz async del grafo compilado que usa el worker.

    `previo` es el snapshot que ve el worker antes de invocar (por defecto, un hilo nuevo).
    """

    def __init__(self, interrupts=(), respuesta="respuesta final", error=None, previo=HILO_VACIO):
        self.interrupts = list(interrupts)
        self.respuesta = respuesta
        self.error = error
        self.previo = previo
        self.entradas = []

    async def ainvoke(self, entrada, config):
        self.entradas.append((entrada, config))
        if self.error:
            raise self.error

    async def aget_state(self, config):
        if not self.entradas:
            return self.previo
        return SimpleNamespace(
            interrupts=self.interrupts,
            next=("aprobacion_humana",) if self.interrupts else (),
            values={"messages": [AIMessage(content=self.respuesta)]},
        )


async def _job_en_cola(r, pregunta="¿Puedo tener mascota?") -> tuple[str, dict]:
    job_id = await jobs.crear_job(r, pregunta)
    _, crudo = await r.brpop([jobs.COLA], timeout=1)
    return job_id, json.loads(crudo)


async def test_exito_queda_done_con_respuesta(r):
    job_id, mensaje = await _job_en_cola(r)
    grafo = GrafoFalso()

    await worker.procesar_mensaje(r, grafo, mensaje)

    job = await jobs.obtener_job(r, job_id)
    assert job["status"] == "DONE"
    assert job["respuesta"] == "respuesta final"
    entrada, config = grafo.entradas[0]
    assert entrada["messages"][0].content == "¿Puedo tener mascota?"
    assert config == {"configurable": {"thread_id": job_id}}


async def test_excepcion_queda_failed_con_error(r):
    job_id, mensaje = await _job_en_cola(r)

    await worker.procesar_mensaje(r, GrafoFalso(error=RuntimeError("boom")), mensaje)

    job = await jobs.obtener_job(r, job_id)
    assert job["status"] == "FAILED"
    assert "RuntimeError" in job["error"] and "boom" in job["error"]


async def test_excepcion_al_reanudar_queda_failed(r):
    job_id, _ = await _job_en_cola(r)
    mensaje = {"job_id": job_id, "tipo": "resume", "resume": {"aprobado": True, "comentario": ""}}
    en_pausa = SimpleNamespace(interrupts=[SimpleNamespace(value={})], next=("aprobacion_humana",), values={"x": 1})
    grafo = GrafoFalso(error=ConnectionError("Redis se cayó"), previo=en_pausa)

    await worker.procesar_mensaje(r, grafo, mensaje)

    job = await jobs.obtener_job(r, job_id)
    assert job["status"] == "FAILED"
    assert "Redis se cayó" in job["error"]
    entrada, _ = grafo.entradas[0]
    assert isinstance(entrada, Command) and entrada.resume == {"aprobado": True, "comentario": ""}


async def test_interrupt_queda_waiting_approval(r):
    job_id, mensaje = await _job_en_cola(r)
    pendiente = SimpleNamespace(value={"accion": "Registrar mudanza", "razon": "r", "pregunta": "p"})

    await worker.procesar_mensaje(r, GrafoFalso(interrupts=[pendiente]), mensaje)

    job = await jobs.obtener_job(r, job_id)
    assert job["status"] == "WAITING_APPROVAL"
    assert job["aprobacion_pendiente"]["accion"] == "Registrar mudanza"
    assert job["respuesta"] is None


async def test_mensaje_de_job_inexistente_no_crea_job(r):
    await worker.procesar_mensaje(r, GrafoFalso(), {"job_id": "fantasma", "tipo": "start"})
    assert await jobs.obtener_job(r, "fantasma") is None


async def test_bucle_worker_consume_la_cola_y_se_cancela_limpio(r):
    job_id = await jobs.crear_job(r, "x")
    tarea = asyncio.create_task(worker.bucle_worker(r, GrafoFalso(), "w-test"))

    for _ in range(40):
        if (await jobs.obtener_job(r, job_id))["status"] == "DONE":
            break
        await asyncio.sleep(0.05)
    tarea.cancel()
    await asyncio.gather(tarea, return_exceptions=True)

    assert (await jobs.obtener_job(r, job_id))["status"] == "DONE"
    assert tarea.cancelled()


async def test_bucle_worker_ignora_mensaje_corrupto(r):
    await r.lpush(jobs.COLA, "esto no es json")
    job_id = await jobs.crear_job(r, "x")
    tarea = asyncio.create_task(worker.bucle_worker(r, GrafoFalso(), "w-test"))

    for _ in range(40):
        if (await jobs.obtener_job(r, job_id))["status"] == "DONE":
            break
        await asyncio.sleep(0.05)
    tarea.cancel()
    await asyncio.gather(tarea, return_exceptions=True)

    assert (await jobs.obtener_job(r, job_id))["status"] == "DONE"


# --- Ciclo completo con el grafo real (nodos falsos) y la cola de Redis ----------

def _supervisor(state):
    if any(c["agente"] == "gestor" for c in state.get("contribuciones", [])):
        return {"next_agent": "FINISH", "instruccion": "", "razon": "fake", "task_completed": True}
    return {"next_agent": "gestor", "instruccion": "Registrar mudanza", "razon": "pedida", "task_completed": False}


def _gestor(state):
    return {"contribuciones": [{"agente": "gestor", "instruccion": "x", "aporte": "registrada"}], "pasos": 1}


def _sintesis(state):
    return {"messages": [AIMessage(content=f"Final: {state['contribuciones'][-1]['aporte']}")]}


async def test_ciclo_hitl_completo_por_la_cola(r):
    grafo = construir_grafo(InMemorySaver(), nodos={"supervisor": _supervisor, "gestor": _gestor, "sintesis": _sintesis})
    job_id, mensaje = await _job_en_cola(r, "Registra mi mudanza")

    await worker.procesar_mensaje(r, grafo, mensaje)
    assert (await jobs.obtener_job(r, job_id))["status"] == "WAITING_APPROVAL"

    assert await jobs.solicitar_reanudacion(r, job_id, True, "")
    _, crudo = await r.brpop([jobs.COLA], timeout=1)
    await worker.procesar_mensaje(r, grafo, json.loads(crudo))

    job = await jobs.obtener_job(r, job_id)
    assert job["status"] == "DONE"
    assert job["respuesta"] == "Final: registrada"


# --- Recuperación tras un reinicio de la API -----------------------------------

PREGUNTA = "¿Puedo tener mascota?"
EN_PAUSA = SimpleNamespace(interrupts=[SimpleNamespace(value={})], next=("aprobacion_humana",), values={"x": 1})
A_MEDIAS = SimpleNamespace(interrupts=[], next=("sintesis",), values={"x": 1})
TERMINADO = SimpleNamespace(interrupts=[], next=(), values={"x": 1})
START = {"job_id": "j", "tipo": "start"}
RESUME = {"job_id": "j", "tipo": "resume", "resume": {"aprobado": True, "comentario": ""}}


def test_elegir_entrada_hilo_nuevo_parte_con_estado_inicial():
    invocar, entrada = worker.elegir_entrada(HILO_VACIO, START, PREGUNTA)
    assert invocar and entrada["messages"][0].content == PREGUNTA


def test_elegir_entrada_reanuda_la_pausa_con_la_decision():
    invocar, entrada = worker.elegir_entrada(EN_PAUSA, RESUME, PREGUNTA)
    assert invocar and isinstance(entrada, Command) and entrada.resume == RESUME["resume"]


@pytest.mark.parametrize("mensaje", [START, RESUME])
def test_elegir_entrada_a_medias_continua_desde_el_checkpoint(mensaje):
    assert worker.elegir_entrada(A_MEDIAS, mensaje, PREGUNTA) == (True, None)


def test_elegir_entrada_hilo_terminado_no_reinvoca():
    assert worker.elegir_entrada(TERMINADO, START, PREGUNTA) == (False, None)


async def test_bucle_worker_confirma_el_mensaje_al_terminar(r):
    job_id = await jobs.crear_job(r, "x")
    tarea = asyncio.create_task(worker.bucle_worker(r, GrafoFalso(), "w-test"))

    for _ in range(40):
        if (await jobs.obtener_job(r, job_id))["status"] == "DONE":
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.05)  # deja que el worker alcance a confirmar
    tarea.cancel()
    await asyncio.gather(tarea, return_exceptions=True)

    assert await r.llen(jobs.COLA) == 0
    assert await r.llen(jobs.PROCESANDO) == 0


class GrafoQueSeCuelga(GrafoFalso):
    """Simula un job largo: queda esperando hasta que el worker se cancela (la API se apaga)."""

    async def ainvoke(self, entrada, config):
        self.entradas.append((entrada, config))
        await asyncio.Event().wait()


async def test_apagado_a_mitad_de_job_deja_el_mensaje_para_recuperarlo(r):
    job_id = await jobs.crear_job(r, "x")
    tarea = asyncio.create_task(worker.bucle_worker(r, GrafoQueSeCuelga(), "w-test"))
    for _ in range(40):
        if (await jobs.obtener_job(r, job_id))["status"] == "RUNNING":
            break
        await asyncio.sleep(0.05)

    tarea.cancel()  # lo que hace el lifespan al apagar la API
    await asyncio.gather(tarea, return_exceptions=True)
    assert (await jobs.obtener_job(r, job_id))["status"] == "RUNNING"
    assert await r.llen(jobs.PROCESANDO) == 1

    # Arranque siguiente: el job vuelve a la cola y otro worker lo termina.
    assert await jobs.recuperar_huerfanos(r) == [job_id]
    tarea = asyncio.create_task(worker.bucle_worker(r, GrafoFalso(), "w-nuevo"))
    for _ in range(40):
        if (await jobs.obtener_job(r, job_id))["status"] == "DONE":
            break
        await asyncio.sleep(0.05)
    tarea.cancel()
    await asyncio.gather(tarea, return_exceptions=True)
    assert (await jobs.obtener_job(r, job_id))["status"] == "DONE"


class NodosContados:
    """Supervisor que termina y síntesis eco, contando cuántas veces corre el supervisor."""

    def __init__(self):
        self.supervisor_llamadas = 0

    def supervisor(self, state):
        self.supervisor_llamadas += 1
        return {"next_agent": "FINISH", "instruccion": "", "razon": "fake", "task_completed": True}

    def sintesis(self, state):
        return {"messages": [AIMessage(content=f"eco: {state['messages'][0].content}")]}

    def grafo(self, saver):
        return construir_grafo(saver, nodos={"supervisor": self.supervisor, "sintesis": self.sintesis})


async def _verificar_retomado(r, grafo, nodos: NodosContados, job_id: str) -> None:
    job = await jobs.obtener_job(r, job_id)
    assert job["status"] == "DONE" and job["respuesta"] == f"eco: {PREGUNTA}"
    assert nodos.supervisor_llamadas == 1  # no se repitió el nodo que ya había terminado
    mensajes = (await grafo.aget_state({"configurable": {"thread_id": job_id}})).values["messages"]
    assert [m.content for m in mensajes] == [PREGUNTA, f"eco: {PREGUNTA}"]  # la pregunta no se duplicó


async def test_job_interrumpido_retoma_sin_repetir_nodos(r):
    """Con el grafo real: el 'reinicio' ocurre después del supervisor; al retomar solo corre la síntesis."""
    nodos = NodosContados()
    grafo = nodos.grafo(InMemorySaver())
    job_id, mensaje = await _job_en_cola(r, PREGUNTA)
    # La API "muere" justo después de que el supervisor dejó su checkpoint.
    await grafo.ainvoke(worker.estado_inicial(PREGUNTA), {"configurable": {"thread_id": job_id}},
                        interrupt_after=["supervisor"])

    await worker.procesar_mensaje(r, grafo, mensaje)  # el mismo mensaje, recuperado tras el reinicio

    await _verificar_retomado(r, grafo, nodos, job_id)


@pytest.mark.redis
async def test_job_interrumpido_retoma_desde_checkpoints_en_redis(r, redis_url):
    """Igual que el anterior, pero con AsyncRedisSaver y dos 'vidas' de la API.

    Cerrar el checkpointer y abrir uno nuevo es lo que pasa en un reinicio real: el estado del
    hilo solo puede venir de Redis, no de la memoria del proceso anterior.
    """
    nodos = NodosContados()
    job_id, mensaje = await _job_en_cola(r, PREGUNTA)

    async with checkpointer_redis(redis_url) as saver:  # primera vida: muere tras el supervisor
        await nodos.grafo(saver).ainvoke(worker.estado_inicial(PREGUNTA),
                                         {"configurable": {"thread_id": job_id}},
                                         interrupt_after=["supervisor"])

    async with checkpointer_redis(redis_url) as saver:  # segunda vida: retoma el mismo hilo
        grafo = nodos.grafo(saver)
        await worker.procesar_mensaje(r, grafo, mensaje)
        await _verificar_retomado(r, grafo, nodos, job_id)
