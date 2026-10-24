from contextlib import asynccontextmanager
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.graph import StateGraph, START, END
from langgraph.graph.state import CompiledStateGraph
from typing import AsyncIterator, Callable, Literal
import logging

from app import config
from app.state import AgentState
from app.agents.supervisor import nodo_supervisor
from app.agents.supervisor import nodo_sintesis
from app.agents.analyst_agent import nodo_analista
from app.agents.research_agent import nodo_investigador
from app.agents.gestor_agent import nodo_gestor
from app.hitl import es_critica, nodo_aprobacion_humana

logger = logging.getLogger(__name__)


def enrutar(state: AgentState) -> Literal["investigador", "analista", "aprobacion_humana", "sintesis"]:
    """Traduce la decisión del supervisor al nombre del próximo nodo.

    Antes de despachar, revisa si la decisión es crítica: en ese caso pasa primero por la aprobación humana.
    El gestor no es un destino directo: solo se llega a él desde aprobacion_humana.
    """
    if es_critica(state):
        return "aprobacion_humana"
    siguiente = state.get("next_agent")
    if siguiente in ("investigador", "analista"):
        return siguiente
    return "sintesis"


@asynccontextmanager
async def checkpointer_redis(url: str = config.settings.redis_url) -> AsyncIterator[AsyncRedisSaver]:
    """Abre el checkpointer de Redis y crea sus índices (requiere RedisJSON y RediSearch)."""
    ttl = {"default_ttl": config.settings.checkpoint_ttl_min, "refresh_on_read": True}
    async with AsyncRedisSaver.from_conn_string(url, ttl=ttl) as saver:
        await saver.asetup()
        yield saver


def construir_grafo(
    checkpointer: BaseCheckpointSaver | None = None,
    nodos: dict[str, Callable] | None = None,
) -> CompiledStateGraph:
    """Arma el orquestador y lo compila con el checkpointer recibido (Redis en la API).

    `nodos` reemplaza nodos por nombre; los tests lo usan para no llamar al LLM.
    """
    implementaciones = {
        "investigador": nodo_investigador,
        "analista": nodo_analista,
        "sintesis": nodo_sintesis,
        "supervisor": nodo_supervisor,
        "gestor": nodo_gestor,
        "aprobacion_humana": nodo_aprobacion_humana,
        **(nodos or {}),
    }
    grafo = StateGraph(AgentState)
    for nombre, funcion in implementaciones.items():
        grafo.add_node(nombre, funcion)
    grafo.add_edge(START, "supervisor")
    grafo.add_conditional_edges("supervisor", enrutar)
    grafo.add_edge("investigador", "supervisor")
    grafo.add_edge("analista", "supervisor")
    grafo.add_edge("gestor", "supervisor")
    # aprobacion_humana no tiene aristas fijas: devuelve Command(goto="gestor" | "supervisor").
    grafo.add_edge("sintesis", END)
    return grafo.compile(checkpointer=checkpointer)


def exportar_diagrama():
    """Exporta el diagrama del grafo a un archivo visualizable."""
    dibujo = construir_grafo().get_graph()
    with open(config.BASE_DIR / "diagrama.md", "w", encoding="utf-8") as archivo:
        archivo.write(f"```mermaid\n{dibujo.draw_mermaid()}```\n")
    try:
        dibujo.draw_mermaid_png(output_file_path=str(config.BASE_DIR / "diagrama.png"))
    except Exception as e:
        logger.warning("No se pudo generar el PNG (requiere internet): %s", e)


if __name__ == "__main__":
    exportar_diagrama()
