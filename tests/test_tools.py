"""Contratos de las herramientas (args_schema + artefactos), idempotencia y resiliencia."""
import asyncio

import pytest
from langchain_core.documents import Document
from langchain_core.language_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import ValidationError

from app.agents import tools
from app.agents.base import POLITICA_REINTENTO_NODOS, crear_agente, es_error_transitorio
from app.agents.schemas import ResultadoBusqueda, ResultadoCalculo, SolicitudNueva, SolicitudRegistrada
from app.graph.builder import construir_grafo
from app.graph.state import nueva_contribucion


def _llamada(nombre: str, args: dict) -> dict:
    """Invocar una tool con un ToolCall (como lo hace el agente) devuelve un ToolMessage con artefacto."""
    return {"type": "tool_call", "name": nombre, "args": args, "id": "llamada-1"}


@pytest.fixture
def redis_tools(monkeypatch, r):
    monkeypatch.setattr(tools, "_redis", None)
    tools.configurar_redis(r)
    return r


# --- Contratos de entrada: el LLM recibe el error y corrige ------------------------------

@pytest.mark.parametrize("expresion", ["5 UTM * 2", "import os", "", "x" * 201])
def test_calculadora_rechaza_lo_que_no_es_aritmetica(expresion):
    with pytest.raises(ValidationError):
        tools.calculadora.invoke({"expresion": expresion})


@pytest.mark.parametrize("args", [{"tipo": "mudanza", "detalle": "sábado"},       # detalle muy corto
                                  {"tipo": "fiesta", "detalle": "sábado 10:00, depto 101"}])
async def test_registrar_solicitud_rechaza_argumentos_fuera_de_contrato(redis_tools, args):
    with pytest.raises(ValidationError):
        await tools.registrar_solicitud.ainvoke(args)
    assert await redis_tools.llen(tools.CLAVE_SOLICITUDES) == 0


# --- Contratos de salida: artefactos tipados -------------------------------------------

def test_calculadora_devuelve_texto_y_artefacto():
    mensaje = tools.calculadora.invoke(_llamada("calculadora", {"expresion": "2 + 4 * 2"}))

    assert isinstance(mensaje, ToolMessage) and mensaje.content == "2 + 4 * 2 = 10"
    assert mensaje.artifact == ResultadoCalculo(expresion="2 + 4 * 2", resultado=10)


def test_calculadora_division_por_cero_queda_en_el_artefacto():
    artefacto = tools.calculadora.invoke(_llamada("calculadora", {"expresion": "1 / 0"})).artifact
    assert artefacto.resultado is None and "cero" in artefacto.error


class RAGFalso:
    async def aretrieve(self, pregunta):
        return [Document("Mudanzas: sábados de 09:00 a 14:00.",
                         metadata={"chunk_id": "obras_mudanzas-003", "source": "obras_mudanzas.txt",
                                   "seccion": "Protocolo de Mudanzas", "relevance_score": 0.91})]


async def test_buscar_devuelve_fragmentos_con_fuente(monkeypatch):
    monkeypatch.setattr(tools, "get_rag", lambda: RAGFalso())

    mensaje = await tools.buscar_en_reglamento.ainvoke(_llamada("buscar_en_reglamento", {"pregunta": "mudanzas"}))

    assert "[Fuente: obras_mudanzas.txt | Sección: Protocolo de Mudanzas | Relevancia: 0.91]" in mensaje.content
    assert isinstance(mensaje.artifact, ResultadoBusqueda)
    fragmento = mensaje.artifact.fragmentos[0]
    assert (fragmento.fuente, fragmento.relevancia) == ("obras_mudanzas.txt", 0.91)


# --- Idempotencia de registrar_solicitud ---------------------------------------------

MUDANZA = SolicitudNueva(tipo="mudanza", detalle="sábado 10:00 a 14:00, depto 101")


async def test_misma_solicitud_del_mismo_job_se_registra_una_vez(r):
    primera = await tools.guardar_solicitud(r, MUDANZA, "job-1")
    segunda = await tools.guardar_solicitud(r, MUDANZA.model_copy(update={"detalle": "sábado 10 a 14, depto 101"}), "job-1")

    assert await r.llen(tools.CLAVE_SOLICITUDES) == 1
    assert (primera.numero, primera.repetida) == (1, False)
    assert (segunda.numero, segunda.repetida) == (1, True)
    assert segunda.detalle == primera.detalle  # se devuelve la original, no la del reintento


async def test_otro_job_u_otro_tipo_si_registran(r):
    await tools.guardar_solicitud(r, MUDANZA, "job-1")
    otra_ejecucion = await tools.guardar_solicitud(r, MUDANZA, "job-2")
    reclamo = await tools.guardar_solicitud(r, SolicitudNueva(tipo="reclamo", detalle="ruidos del depto 202"), "job-1")

    assert (otra_ejecucion.numero, reclamo.numero) == (2, 3)


async def test_reintentos_concurrentes_registran_una_sola_vez(r):
    """WATCH + MULTI: aunque 5 intentos del mismo job lleguen a la vez, queda un solo registro."""
    resultados = await asyncio.gather(*(tools.guardar_solicitud(r, MUDANZA, "job-1") for _ in range(5)))

    assert await r.llen(tools.CLAVE_SOLICITUDES) == 1
    assert {s.numero for s in resultados} == {1}
    assert sum(not s.repetida for s in resultados) == 1


async def test_la_tool_usa_el_thread_id_como_clave_de_idempotencia(redis_tools):
    """run_config lo inyecta LangChain: dentro del grafo, su thread_id es el job_id."""
    llamada = _llamada("registrar_solicitud", {"tipo": "mudanza", "detalle": "sábado 10:00, depto 101"})
    config = {"configurable": {"thread_id": "job-7"}}

    primera = await tools.registrar_solicitud.ainvoke(llamada, config=config)
    repetida = await tools.registrar_solicitud.ainvoke(llamada, config=config)

    assert isinstance(primera.artifact, SolicitudRegistrada) and primera.artifact.job_id == "job-7"
    assert repetida.artifact.repetida and "ya estaba registrada con el n.º 1" in repetida.content
    assert await redis_tools.llen(tools.CLAVE_SOLICITUDES) == 1


# --- Contribuciones validadas ----------------------------------------------------------

def test_contribucion_valida_y_normaliza():
    assert nueva_contribucion("analista", " calcula ", " 10 UTM ") == {
        "agente": "analista", "instruccion": "calcula", "aporte": "10 UTM"}


@pytest.mark.parametrize("agente, aporte", [("analista", "   "), ("gerente", "algo")])
def test_contribucion_invalida_falla(agente, aporte):
    with pytest.raises(ValidationError):
        nueva_contribucion(agente, "tarea", aporte)


# --- Resiliencia --------------------------------------------------------------------

def test_politica_de_reintentos_por_tipo_de_error():
    assert es_error_transitorio(ConnectionError("red caída"))
    assert not es_error_transitorio(ValueError("bug"))
    assert not es_error_transitorio(TypeError("bug"))


def test_nodos_con_llm_tienen_reintento_y_la_aprobacion_no():
    nodos = construir_grafo().builder.nodes
    assert nodos["supervisor"].retry_policy == POLITICA_REINTENTO_NODOS
    assert nodos["gestor"].retry_policy == POLITICA_REINTENTO_NODOS
    assert nodos["aprobacion_humana"].retry_policy is None


class ModeloConHerramientas(GenericFakeChatModel):
    """LLM falso que acepta bind_tools: primero llama a la tool y luego responde con lo que obtuvo."""

    def bind_tools(self, tools, **kwargs):
        return self


async def test_herramienta_caida_llega_al_agente_como_mensaje_y_no_rompe_el_grafo():
    llamadas = []

    @tool
    async def consultar_servicio(pregunta: str) -> str:
        """Servicio externo que está caído."""
        llamadas.append(pregunta)
        raise ConnectionError("503 Service Unavailable")

    modelo = ModeloConHerramientas(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "consultar_servicio", "args": {"pregunta": "x"}, "id": "1"}]),
        AIMessage(content="No pude consultar el reglamento en este momento."),
    ]))
    agente = crear_agente([consultar_servicio], "prompt", modelo=modelo)

    resultado = await agente.ainvoke({"messages": [("human", "hola")]})

    assert len(llamadas) == 3  # intento inicial + 2 reintentos con backoff
    error = next(m for m in resultado["messages"] if isinstance(m, ToolMessage))
    assert error.status == "error" and "503" in error.content
    assert resultado["messages"][-1].content == "No pude consultar el reglamento en este momento."


async def test_agente_corrige_argumentos_invalidos_con_el_error_del_contrato():
    """El ToolNode le devuelve al modelo el error de args_schema; el modelo corrige y reintenta."""
    modelo = ModeloConHerramientas(messages=iter([
        AIMessage(content="", tool_calls=[{"name": "calculadora", "args": {"expresion": "5 UTM * 2"}, "id": "1"}]),
        AIMessage(content="", tool_calls=[{"name": "calculadora", "args": {"expresion": "5 * 2"}, "id": "2"}]),
        AIMessage(content="La multa total es de 10 UTM."),
    ]))
    agente = crear_agente([tools.calculadora], "prompt", modelo=modelo)

    resultado = await agente.ainvoke({"messages": [("human", "¿Cuánto son dos multas de 5 UTM?")]})

    rechazo, calculo = [m for m in resultado["messages"] if isinstance(m, ToolMessage)]
    assert rechazo.status == "error" and "expresion" in rechazo.content  # el modelo ve qué campo falló
    assert calculo.artifact == ResultadoCalculo(expresion="5 * 2", resultado=10)
    assert resultado["messages"][-1].content == "La multa total es de 10 UTM."
