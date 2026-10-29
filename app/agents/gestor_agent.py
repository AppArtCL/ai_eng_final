import logging

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.graph.state import AgentState
from app.agents.base import crear_agente
from app.agents.tools import registrar_solicitud

logger = logging.getLogger(__name__)

PROMPT_GESTOR = """Eres el agente gestor del Edificio Riesco 6540. Ejecutas acciones ante la administración.
Cuando recibes una instrucción, la aprobación humana YA ocurrió: no pidas confirmación ni autorización a nadie.
Registra exactamente la solicitud indicada en la instrucción, una sola vez, con tu herramienta.
Si la instrucción menciona requisitos del reglamento (autorización de la administración, protección del ascensor), inclúyelos en el detalle de la solicitud.
No inventes datos: si falta fecha, hora o departamento, no registres nada e informa qué falta.
Responde breve, con el número de solicitud que devolvió la herramienta.
"""

agente_gestor = crear_agente([registrar_solicitud], PROMPT_GESTOR)


async def nodo_gestor(state: AgentState) -> dict:
    """Ejecuta la acción aprobada y verifica en código que la herramienta realmente se usó."""
    instruccion = state.get("instruccion") or state["messages"][0].content
    mensaje = f"Acción APROBADA por un humano. Ejecútala ahora con tu herramienta:\n{instruccion}"
    resultado = await agente_gestor.ainvoke({"messages": [HumanMessage(content=mensaje)]})
    respuesta = resultado["messages"][-1].content

    # Validación determinista: sin ToolMessage no hubo registro, diga lo que diga el LLM.
    if not any(isinstance(m, ToolMessage) for m in resultado["messages"]):
        logger.warning("El gestor respondió sin usar registrar_solicitud: %s", respuesta)
        respuesta = f"No se registró ninguna solicitud. Respuesta del gestor: {respuesta}"
    return {
        "messages": [AIMessage(content=respuesta, name="gestor")],
        "contribuciones": [{"agente": "gestor", "instruccion": instruccion, "aporte": respuesta}],
        "pasos": 1,
    }
