"""El nodo supervisor usa la cadena estructurada y traduce su decisión al estado del grafo."""
from langchain_core.runnables import RunnableLambda

from app import config
from app.agents import supervisor
from app.agents.supervisor import DecisionSupervisor
from app.graph.state import estado_inicial


def _cadena_falsa(decision: DecisionSupervisor, entradas: list):
    """Reemplaza la cadena del LLM: guarda lo que recibe y devuelve una decisión fija."""
    def responder(entrada):
        entradas.append(entrada)
        return decision
    return RunnableLambda(responder)


async def test_decision_de_la_cadena_pasa_al_estado(monkeypatch):
    entradas = []
    decision = DecisionSupervisor(next="investigador", instruccion="Busca el horario de mudanzas", razon="paso 1")
    monkeypatch.setattr(supervisor, "cadena_supervisor", _cadena_falsa(decision, entradas))
    estado = {**estado_inicial("¿Puedo mudarme el sábado?"),
              "contribuciones": [{"agente": "analista", "instruccion": "x", "aporte": "12 horas"}]}

    resultado = await supervisor.nodo_supervisor(estado)

    assert resultado == {"next_agent": "investigador", "instruccion": "Busca el horario de mudanzas",
                         "razon": "paso 1", "task_completed": False}
    contexto = entradas[0]["contexto"]
    assert "¿Puedo mudarme el sábado?" in contexto and "12 horas" in contexto


async def test_finish_marca_la_tarea_completa(monkeypatch):
    decision = DecisionSupervisor(next="FINISH", instruccion="", razon="rúbrica 4")
    monkeypatch.setattr(supervisor, "cadena_supervisor", _cadena_falsa(decision, []))

    assert (await supervisor.nodo_supervisor(estado_inicial("x")))["task_completed"] is True


async def test_limite_de_pasos_corta_sin_llamar_al_llm(monkeypatch):
    entradas = []
    monkeypatch.setattr(supervisor, "cadena_supervisor",
                        _cadena_falsa(DecisionSupervisor(next="analista", instruccion="", razon=""), entradas))
    estado = {**estado_inicial("x"), "pasos": config.settings.max_pasos}

    resultado = await supervisor.nodo_supervisor(estado)

    assert resultado["next_agent"] == "FINISH" and resultado["task_completed"] is True
    assert entradas == []


def test_llaves_en_la_pregunta_no_rompen_la_plantilla():
    pregunta = "¿Qué significa {multa} en el reglamento?"
    mensajes = supervisor.PROMPT_DECISION.format_messages(contexto=pregunta)

    assert mensajes[0].content == supervisor.PROMPT_SUPERVISOR
    assert mensajes[1].content == pregunta
