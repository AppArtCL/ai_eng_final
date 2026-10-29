"""Cadena LCEL de texto libre (mensajes → LLM → str), con modelo de respaldo."""
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import Runnable

from app import config


def crear_cadena_texto(
    llm: BaseChatModel | None = None,
    respaldo: BaseChatModel | None = None,
) -> Runnable[list[BaseMessage], str]:
    """LLM | StrOutputParser, con with_fallbacks hacia el modelo de respaldo.

    Recibe la lista de mensajes ya armada (no una plantilla): así quien la usa puede continuar
    una conversación, por ejemplo para pedirle al modelo que corrija su respuesta anterior.
    Misma regla que crear_cadena_estructurada: sin `llm` se usan los modelos de la configuración.
    """
    if llm is None:
        llm, respaldo = config.get_llm(), respaldo or config.get_llm_respaldo()
    cadena: Runnable[list[BaseMessage], str] = llm | StrOutputParser()
    if respaldo is not None:
        cadena = cadena.with_fallbacks([respaldo | StrOutputParser()])
    return cadena
