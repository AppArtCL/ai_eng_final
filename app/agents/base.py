"""Fábrica común de los agentes ReAct (investigador, analista, gestor) y su política de resiliencia."""
from collections.abc import Callable, Sequence
from typing import cast

from langchain.agents import create_agent
from langchain.agents.middleware import ModelFallbackMiddleware, ToolRetryMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.types import RetryPolicy

from app import config

# Un solo criterio de "error transitorio" para todo el sistema: el de LangGraph. Reintenta errores
# de red, 5xx y errores de los SDK (OpenAI, Pinecone); no reintenta errores de lógica
# (ValueError, TypeError, KeyError...), que fallarían igual en el siguiente intento.
POLITICA_REINTENTO_NODOS = RetryPolicy(max_attempts=2)
# RetryPolicy acepta varias formas de retry_on; la que trae por defecto es una función.
es_error_transitorio = cast(Callable[[Exception], bool], POLITICA_REINTENTO_NODOS.retry_on)


def crear_agente(herramientas: Sequence[BaseTool], prompt: str, modelo: BaseChatModel | None = None):
    """Agente ReAct con resiliencia en sus dos tipos de llamada externa.

    - Herramientas (ToolRetryMiddleware): reintenta los errores transitorios con backoff y, si
      siguen fallando, le entrega el error al agente como ToolMessage ("continue") en vez de romper
      el grafo. El agente puede reformular, decir que no pudo consultar, o el supervisor reasignar.
      Los argumentos inválidos no llegan aquí: el ToolNode ya le devuelve al modelo el error de
      validación de args_schema para que corrija y vuelva a llamar.
    - Modelo (ModelFallbackMiddleware): create_agent recibe un modelo, no una cadena, así que
      with_fallbacks no aplica; si el principal falla tras sus reintentos, se usa el de respaldo.

    `modelo` reemplaza al de la configuración (los tests usan uno falso).
    """
    middleware: list = [
        ToolRetryMiddleware(max_retries=2, retry_on=es_error_transitorio, on_failure="continue"),
    ]
    if respaldo := config.get_llm_respaldo():
        middleware.append(ModelFallbackMiddleware(respaldo))
    return create_agent(
        model=modelo or config.get_llm(),
        tools=list(herramientas),
        system_prompt=prompt,
        middleware=middleware,
    )
