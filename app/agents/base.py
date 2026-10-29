"""Fábrica común de los agentes ReAct (investigador, analista, gestor)."""
from collections.abc import Sequence

from langchain.agents import create_agent
from langchain.agents.middleware import ModelFallbackMiddleware
from langchain_core.tools import BaseTool

from app import config


def crear_agente(herramientas: Sequence[BaseTool], prompt: str):
    """Agente ReAct con el modelo principal y, si está configurado, el de respaldo.

    create_agent recibe un modelo, no una cadena, así que with_fallbacks no aplica: el respaldo
    se declara con ModelFallbackMiddleware, que repite la llamada con el otro modelo si la del
    principal lanza una excepción (después de los reintentos del cliente de OpenAI).
    """
    respaldo = config.get_llm_respaldo()
    return create_agent(
        model=config.get_llm(),
        tools=list(herramientas),
        system_prompt=prompt,
        middleware=[ModelFallbackMiddleware(respaldo)] if respaldo else [],
    )
