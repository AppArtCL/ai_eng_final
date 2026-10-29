import logging

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from app.agents.base import crear_agente
from app.agents.schemas import SolicitudRegistrada
from app.graph.state import AgentState, nueva_contribucion
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


def _como_solicitud(artefacto) -> SolicitudRegistrada | None:
    """Valida el artefacto de registrar_solicitud, venga como objeto o restaurado de un checkpoint.

    Si el subgrafo del gestor se retoma tras un reinicio, sus mensajes vuelven del checkpoint de
    Redis y el artefacto llega como dict, en la forma serializada de LangChain
    ({"lc": 2, "type": "constructor", "kwargs": {...}}): el serializador no reconstruye clases de
    la app. Se vuelve a validar con Pydantic en vez de confiar en el tipo.
    """
    if isinstance(artefacto, SolicitudRegistrada):
        return artefacto
    if isinstance(artefacto, dict):
        try:
            return SolicitudRegistrada.model_validate(artefacto.get("kwargs", artefacto))
        except ValidationError:
            return None
    return None


def solicitudes_registradas(mensajes: list) -> list[SolicitudRegistrada]:
    """Los registros que la herramienta realmente hizo, tomados de sus artefactos validados."""
    candidatas = (_como_solicitud(m.artifact) for m in mensajes
                  if isinstance(m, ToolMessage) and m.name == registrar_solicitud.name)
    return [s for s in candidatas if s is not None]


async def nodo_gestor(state: AgentState) -> dict:
    """Ejecuta la acción aprobada y verifica en código, con el artefacto de la herramienta, qué se registró."""
    instruccion = state.get("instruccion") or state["messages"][0].text
    mensaje = f"Acción APROBADA por un humano. Ejecútala ahora con tu herramienta:\n{instruccion}"
    resultado = await agente_gestor.ainvoke({"messages": [HumanMessage(content=mensaje)]})
    respuesta = resultado["messages"][-1].text

    # Validación determinista: lo que cuenta es el artefacto de registrar_solicitud, no lo que
    # diga el LLM. El número de solicitud sale del registro real, nunca del texto del modelo.
    registradas = solicitudes_registradas(resultado["messages"])
    if registradas:
        detalle = "; ".join(f"{s.tipo} n.º {s.numero} ({s.detalle})" for s in registradas)
        aporte = f"Solicitud registrada: {detalle}. Respuesta del gestor: {respuesta}"
    else:
        logger.warning("El gestor respondió sin registrar ninguna solicitud: %s", respuesta)
        aporte = f"No se registró ninguna solicitud. Respuesta del gestor: {respuesta}"
    return {
        "messages": [AIMessage(content=aporte, name="gestor")],
        "contribuciones": [nueva_contribucion("gestor", instruccion, aporte)],
        "pasos": 1,
    }
