from langchain_core.messages import AIMessage, HumanMessage

from app.graph.state import AgentState
from app.agents.base import crear_agente
from app.agents.tools import calculadora

PROMPT_ANALISTA = """Eres el agente analista del Reglamento de Copropiedad del Edificio Riesco 6540, encargado de hacer todos los cálculos que se requieran.
Debes trabajar con los datos que te entregan, sin buscar o inventar otros datos.
Debes usar la calculadora para realizar cualquier cálculo que se haga, aunque sea sencillo.
Muestra la operación realizada y el resultado, incluyendo las unidades (por ejemplo, m², $, horas, UTM, días).
Si faltan datos para hacer los cálculos, no hagas ningún cálculo e informa explícitamente la falta de datos.
"""

agente_analista = crear_agente([calculadora], PROMPT_ANALISTA)

SIN_DATOS = "(sin datos del investigador)"


def _ultimo_aporte_investigador(state: AgentState) -> str:
    """Devuelve el aporte más reciente del investigador (no el del propio analista)."""
    return next(
        (c["aporte"] for c in reversed(state.get("contribuciones", [])) if c["agente"] == "investigador"),
        SIN_DATOS,
    )


async def nodo_analista(state: AgentState) -> dict:
    """Ejecuta al analista con la instrucción del supervisor y solo los datos del investigador."""
    instruccion = state.get("instruccion") or state["messages"][0].content
    datos = _ultimo_aporte_investigador(state)
    mensaje = f"Tarea: {instruccion}\n\nDatos disponibles:\n{datos}"

    resultado = await agente_analista.ainvoke({"messages": [HumanMessage(content=mensaje)]})
    respuesta = resultado["messages"][-1].content
    return {
        "messages": [AIMessage(content=respuesta, name="analista")],
        "contribuciones": [{"agente": "analista", "instruccion": instruccion, "aporte": respuesta}],
        "pasos": 1,
    }
