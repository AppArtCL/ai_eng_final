import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from app import config
from app.graph import checkpointer_redis, construir_grafo
from app.state import estado_inicial


def supervisor_que_termina(state):
    return {"next_agent": "FINISH", "instruccion": "", "razon": "fake", "task_completed": True}


def sintesis_eco(state):
    return {"messages": [AIMessage(content=f"eco: {state['messages'][0].content}", name="supervisor")]}


FAKES = {"supervisor": supervisor_que_termina, "sintesis": sintesis_eco}


def _cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def test_construir_grafo_contiene_nodos_del_m6():
    nodos = construir_grafo(InMemorySaver()).get_graph().nodes
    assert {"supervisor", "investigador", "analista", "sintesis"} <= set(nodos)


async def _ejecutar_dos_hilos(grafo, prefijo: str) -> None:
    """Corre dos preguntas en hilos distintos y verifica que no se mezclen."""
    await grafo.ainvoke(estado_inicial("pregunta A"), _cfg(f"{prefijo}-a"))
    await grafo.ainvoke(estado_inicial("pregunta B"), _cfg(f"{prefijo}-b"))

    estado_a = (await grafo.aget_state(_cfg(f"{prefijo}-a"))).values
    estado_b = (await grafo.aget_state(_cfg(f"{prefijo}-b"))).values
    assert estado_a["messages"][0].content == "pregunta A"
    assert estado_a["messages"][-1].content == "eco: pregunta A"
    assert estado_b["messages"][0].content == "pregunta B"
    assert estado_b["messages"][-1].content == "eco: pregunta B"


async def test_hilos_aislados_por_thread_id():
    await _ejecutar_dos_hilos(construir_grafo(InMemorySaver(), nodos=FAKES), "mem")


@pytest.mark.redis
async def test_checkpoints_persisten_en_redis(redis_url, prefijo_unico):
    async with checkpointer_redis(redis_url) as saver:
        await _ejecutar_dos_hilos(construir_grafo(saver, nodos=FAKES), prefijo_unico)

    # Un checkpointer nuevo (como tras reiniciar la API) recupera el mismo hilo desde Redis.
    async with checkpointer_redis(redis_url) as saver:
        recuperado = await construir_grafo(saver, nodos=FAKES).aget_state(_cfg(f"{prefijo_unico}-a"))
    assert recuperado.values["messages"][-1].content == "eco: pregunta A"


def test_config_redis_tiene_valores_por_defecto():
    assert config.settings.redis_url.startswith("redis://")
    assert config.settings.num_workers >= 1
    assert config.settings.checkpoint_ttl_min >= 1
