import json

import fakeredis
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents import tools
from app.graph import construir_grafo
from app.hitl import es_critica
from app.state import estado_inicial

PREGUNTA = "Registra una solicitud de mudanza para el sábado a las 10:00, depto 101"


# --- Política de criticidad ---------------------------------------------------

def test_gestor_es_critico():
    assert es_critica({"next_agent": "gestor", "pasos": 1}) is True


def test_investigador_no_es_critico():
    assert es_critica({"next_agent": "investigador", "pasos": 1}) is False


def test_finish_no_es_critico():
    assert es_critica({"next_agent": "FINISH", "pasos": 2}) is False


# --- Herramienta con efecto secundario ----------------------------------------

@pytest.fixture
def redis_falso(monkeypatch):
    cliente = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(tools, "get_redis", lambda: cliente)
    return cliente


def test_registrar_solicitud_escribe_en_redis(redis_falso):
    salida = tools.registrar_solicitud.invoke({"tipo": "mudanza", "detalle": "sábado 10:00, depto 101"})

    guardadas = redis_falso.lrange(tools.CLAVE_SOLICITUDES, 0, -1)
    assert len(guardadas) == 1
    solicitud = json.loads(guardadas[0])
    assert solicitud["tipo"] == "mudanza"
    assert solicitud["detalle"] == "sábado 10:00, depto 101"
    assert "creado_en" in solicitud
    assert "registrada" in salida


# --- Ciclo pausa → decisión humana → reanudación -------------------------------

class GestorEspia:
    """Gestor falso que cuenta cuántas veces se ejecutó."""

    def __init__(self):
        self.llamadas = 0

    def __call__(self, state):
        self.llamadas += 1
        return {
            "contribuciones": [{"agente": "gestor", "instruccion": state["instruccion"], "aporte": "Solicitud registrada"}],
            "pasos": 1,
        }


def supervisor_que_pide_accion(state):
    """Pide la acción una vez; cuando el gestor ya respondió (o fue rechazado), termina."""
    if any(c["agente"] == "gestor" for c in state.get("contribuciones", [])):
        return {"next_agent": "FINISH", "instruccion": "", "razon": "fake", "task_completed": True}
    return {"next_agent": "gestor", "instruccion": "Registrar mudanza sábado 10:00 depto 101",
            "razon": "el vecino pidió la acción", "task_completed": False}


def sintesis_fake(state):
    return {"messages": []}


def _grafo(gestor):
    nodos = {"supervisor": supervisor_que_pide_accion, "gestor": gestor, "sintesis": sintesis_fake}
    return construir_grafo(InMemorySaver(), nodos=nodos)


CFG = {"configurable": {"thread_id": "hitl-1"}}


def test_grafo_incluye_nodos_de_hitl():
    nodos = construir_grafo(InMemorySaver()).get_graph().nodes
    assert {"gestor", "aprobacion_humana"} <= set(nodos)


async def test_interrupt_pausa_antes_del_gestor():
    gestor = GestorEspia()
    grafo = _grafo(gestor)

    await grafo.ainvoke(estado_inicial(PREGUNTA), CFG)

    snap = await grafo.aget_state(CFG)
    assert snap.interrupts, "el grafo debía quedar pausado"
    assert snap.interrupts[0].value["accion"] == "Registrar mudanza sábado 10:00 depto 101"
    assert snap.interrupts[0].value["pregunta"] == PREGUNTA
    assert gestor.llamadas == 0


async def test_aprobacion_ejecuta_gestor_y_termina():
    gestor = GestorEspia()
    grafo = _grafo(gestor)
    await grafo.ainvoke(estado_inicial(PREGUNTA), CFG)

    await grafo.ainvoke(Command(resume={"aprobado": True, "comentario": ""}), CFG)

    final = await grafo.aget_state(CFG)
    assert not final.next and not final.interrupts
    assert gestor.llamadas == 1
    assert final.values["aprobacion"] == {"aprobado": True, "comentario": ""}


async def test_rechazo_no_ejecuta_gestor():
    gestor = GestorEspia()
    grafo = _grafo(gestor)
    await grafo.ainvoke(estado_inicial(PREGUNTA), CFG)

    await grafo.ainvoke(Command(resume={"aprobado": False, "comentario": "ese día hay obras"}), CFG)

    final = await grafo.aget_state(CFG)
    assert not final.next
    assert gestor.llamadas == 0
    rechazo = final.values["contribuciones"][-1]
    assert rechazo["agente"] == "gestor"
    assert "rechazada" in rechazo["aporte"] and "ese día hay obras" in rechazo["aporte"]


# --- Gestor real: debe ejecutar la herramienta o declarar que no lo hizo -----------

class AgenteFalso:
    """Reemplaza al agente LLM del gestor y guarda lo que recibió."""

    def __init__(self, mensajes_respuesta):
        self.mensajes_respuesta = mensajes_respuesta
        self.recibido = None

    def invoke(self, entrada):
        self.recibido = entrada["messages"][0].content
        return {"messages": self.mensajes_respuesta}


ESTADO_GESTOR = {"instruccion": "Registrar mudanza sábado 10:00 depto 101", "messages": [], "contribuciones": []}


def test_gestor_recibe_la_accion_como_aprobada(monkeypatch):
    from langchain_core.messages import AIMessage, ToolMessage
    from app.agents import gestor_agent
    falso = AgenteFalso([ToolMessage(content="Solicitud de mudanza registrada con el n.º 1", tool_call_id="1"),
                         AIMessage(content="Listo, solicitud n.º 1")])
    monkeypatch.setattr(gestor_agent, "agente_gestor", falso)

    salida = gestor_agent.nodo_gestor(ESTADO_GESTOR)

    assert "APROBADA" in falso.recibido
    assert "Registrar mudanza sábado 10:00 depto 101" in falso.recibido
    assert salida["contribuciones"][0]["aporte"] == "Listo, solicitud n.º 1"


def test_gestor_sin_llamar_herramienta_lo_declara(monkeypatch):
    from langchain_core.messages import AIMessage
    from app.agents import gestor_agent
    falso = AgenteFalso([AIMessage(content="He solicitado la aprobación humana, confirma por favor")])
    monkeypatch.setattr(gestor_agent, "agente_gestor", falso)

    salida = gestor_agent.nodo_gestor(ESTADO_GESTOR)

    aporte = salida["contribuciones"][0]["aporte"]
    assert aporte.startswith("No se registró ninguna solicitud")
