"""Estado compartido del orquestador multi-agente.

Cada nodo del grafo lee este estado y devuelve un dict solo con los campos que modifica.
LangGraph fusiona esa actualización con el estado actual: los campos con reducer
se acumulan y los demás se reemplazan.
"""
import operator
from typing import Annotated, List, Literal, Optional, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import MessagesState

# Nombres de los nodos especialistas y decisiones posibles del supervisor.
# Se definen una sola vez para reutilizarlos en el supervisor y en el grafo.
Especialista = Literal["investigador", "analista", "gestor"]
Decision = Literal["investigador", "analista", "gestor", "FINISH"]


class Contribucion(TypedDict):
    """Registro de lo que aportó un especialista en un paso."""
    agente: Especialista
    instruccion: str
    aporte: str


class AgentState(MessagesState):
    """Hereda 'messages' (con su reducer add_messages) y suma campos propios."""

    # Última decisión del supervisor: se reemplaza en cada vuelta.
    next_agent: Optional[Decision]
    # Tarea puntual que el supervisor le encarga al especialista (contexto acotado).
    instruccion: str
    # Justificación de la última decisión del supervisor.
    razon: str
    # Historial de aportes: cada especialista agrega el suyo sin borrar los anteriores.
    contribuciones: Annotated[List[Contribucion], operator.add]
    # Contador de pasos: cada especialista devuelve {"pasos": 1} y el reducer los suma.
    pasos: Annotated[int, operator.add]
    # True cuando el supervisor decide FINISH (o se alcanza MAX_PASOS).
    task_completed: bool
    # Última decisión humana sobre una acción crítica: {"aprobado": bool, "comentario": str}.
    aprobacion: Optional[dict]


def estado_inicial(pregunta: str) -> AgentState:
    """Construye el estado inicial con todos los campos de AgentState."""
    return AgentState(
        messages=[HumanMessage(content=pregunta)],
        next_agent=None,
        instruccion="",
        razon="",
        contribuciones=[],
        pasos=0,
        task_completed=False,
        aprobacion=None,
    )
