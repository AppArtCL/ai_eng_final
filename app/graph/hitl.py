"""Human-in-the-loop: qué se considera crítico y el nodo que pausa el grafo hasta recibir aprobación.

El nodo llama a interrupt(): LangGraph guarda el checkpoint en Redis y la ejecución se detiene.
La API la retoma con Command(resume={"aprobado": bool, "comentario": str}) sobre el mismo thread_id.
"""
import logging
from typing import Literal

from langgraph.types import Command, interrupt

from app.graph.state import AgentState

logger = logging.getLogger(__name__)

# Agentes cuyas herramientas cambian algo fuera del grafo (no solo leen o calculan).
AGENTES_CON_EFECTOS = frozenset({"gestor"})


def es_critica(state: AgentState) -> bool:
    """Decide si la próxima decisión del supervisor requiere aprobación humana.

    Se evalúa en cada vuelta, justo después del supervisor y antes de ir al especialista.
    Campos útiles del estado: state.get("next_agent") (a quién va el trabajo) y
    state.get("pasos") (cuántas vueltas lleva; cada vuelta son más tokens y más costo).

    Un falso negativo ejecuta una acción real sin permiso; un falso positivo solo agrega fricción.
    """
    # Hoy solo el gestor tiene efectos reales. Un agente nuevo con efectos se agrega a este conjunto.
    return state.get("next_agent") in AGENTES_CON_EFECTOS


async def nodo_aprobacion_humana(state: AgentState) -> Command[Literal["gestor", "supervisor"]]:
    """Pausa el grafo y, según la decisión humana, ejecuta la acción o vuelve al supervisor."""
    decision = interrupt({
        "accion": state["instruccion"],
        "razon": state["razon"],
        "pregunta": state["messages"][0].content,
    })
    aprobado = bool(decision.get("aprobado"))
    comentario = decision.get("comentario", "")
    aprobacion = {"aprobado": aprobado, "comentario": comentario}
    logger.info("Decisión humana: %s", aprobacion)

    if aprobado:
        return Command(goto="gestor", update={"aprobacion": aprobacion})

    # Rechazo: queda registrado como aporte para que el supervisor no lo vuelva a intentar
    # y la síntesis pueda informarlo.
    rechazo = {
        "agente": "gestor",
        "instruccion": state["instruccion"],
        "aporte": f"Acción rechazada por el humano. Comentario: {comentario or '(sin comentario)'}",
    }
    return Command(goto="supervisor", update={"aprobacion": aprobacion, "contribuciones": [rechazo], "pasos": 1})
