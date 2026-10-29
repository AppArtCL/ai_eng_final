from langchain_core.messages import AIMessage, HumanMessage

from app.graph.state import AgentState, nueva_contribucion
from app.agents.base import crear_agente
from app.agents.tools import buscar_en_reglamento

PROMPT_INVESTIGADOR = """Eres el agente investigador del Reglamento de Copropiedad del Edificio Riesco 6540.
Solo buscas información con tu herramienta; no haces cálculos ni inventas datos que no aparezcan en los resultados.
Si la primera búsqueda no basta, reformula los términos y vuelve a buscar.
Si el reglamento no contiene el dato pedido, dilo explícitamente.
Responde breve, con los datos concretos (números, horarios, montos) y la fuente de cada uno.
"""

agente_investigador = crear_agente([buscar_en_reglamento], PROMPT_INVESTIGADOR)


async def nodo_investigador(state: AgentState) -> dict:
    """Ejecuta al investigador solo con la instrucción del supervisor (contexto acotado)."""
    instruccion = state.get("instruccion") or state["messages"][0].text
    resultado = await agente_investigador.ainvoke({"messages": [HumanMessage(content=instruccion)]})
    respuesta = resultado["messages"][-1].text
    return {
        "messages": [AIMessage(content=respuesta, name="investigador")],
        "contribuciones": [nueva_contribucion("investigador", instruccion, respuesta)],
        "pasos": 1,
    }