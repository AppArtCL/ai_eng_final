"""Cadenas LCEL con salida estructurada, contrato Pydantic y reintentos (patrón del M2).

    prompt | llm.with_structured_output(Esquema, include_raw=True) | validar_respuesta_completa

Con include_raw=True el modelo no lanza excepción si su salida no calza con el esquema: devuelve
{"raw": AIMessage, "parsed": Esquema | None, "parsing_error": Exception | None}. El último eslabón
revisa esa salida y lanza RespuestaInvalida si no sirve; with_retry vuelve a llamar al LLM.
"""
import logging
from typing import Any, TypeVar, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel

from app import config

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)

# Valores de finish_reason (OpenAI) y stop_reason (Anthropic) que indican una respuesta cortada.
RAZONES_TRUNCADA = {"length", "max_tokens"}
INTENTOS = 3


class RespuestaInvalida(ValueError):
    """La respuesta del LLM no cumple el contrato: vino truncada o no se pudo parsear."""


def validar_respuesta_completa(resultado: dict[str, Any]) -> BaseModel:
    """Devuelve el objeto Pydantic parseado, o lanza RespuestaInvalida si la respuesta no sirve.

    `resultado` es la salida de with_structured_output(..., include_raw=True):
      - resultado["raw"]: el AIMessage crudo; su .response_metadata trae "finish_reason" (OpenAI)
        o "stop_reason" (Anthropic).
      - resultado["parsed"]: el objeto del esquema, o None si el parseo falló.
      - resultado["parsing_error"]: la excepción del parseo, o None.
    """
    metadata = resultado["raw"].response_metadata
    razon = metadata.get("finish_reason") or metadata.get("stop_reason")

    # Primero la truncación: un JSON cortado puede calzar igual con el esquema (por ejemplo, una
    # instrucción a medio escribir sigue siendo un str válido) y pasaría como respuesta correcta.
    if razon in RAZONES_TRUNCADA:
        logger.warning("Respuesta del LLM truncada por límite de tokens (razón: %s); se reintenta.", razon)
        raise RespuestaInvalida(f"Respuesta truncada por límite de tokens (razón: {razon}).")

    if resultado["parsed"] is None:
        error = resultado.get("parsing_error")
        logger.warning("La respuesta del LLM no calza con el esquema: %s", error)
        raise RespuestaInvalida(f"No se pudo parsear la respuesta: {error}")

    return resultado["parsed"]


def crear_cadena_estructurada(
    prompt: ChatPromptTemplate,
    esquema: type[M],
    llm: BaseChatModel | None = None,
) -> Runnable[dict, M]:
    """Arma prompt | LLM estructurado | validación, con reintentos ante respuestas inválidas.

    Solo se reintenta RespuestaInvalida: los errores de red y de rate limit ya los reintenta el
    cliente de OpenAI (max_retries), y un error de autenticación no se arregla reintentando.
    """
    llm = llm or config.get_llm()
    # Con include_raw=True la salida es siempre el dict {"raw", "parsed", "parsing_error"}, pero
    # LangChain la tipa como `dict | BaseModel` (una sola firma para ambos modos).
    estructurado = cast(Runnable[Any, dict[str, Any]], llm.with_structured_output(esquema, include_raw=True))
    cadena = prompt | estructurado | RunnableLambda(validar_respuesta_completa)
    con_reintentos = cadena.with_retry(
        retry_if_exception_type=(RespuestaInvalida,),
        stop_after_attempt=INTENTOS,
        wait_exponential_jitter=True,
    )
    # validar_respuesta_completa devuelve el objeto que parseó with_structured_output(esquema),
    # o sea, una instancia de M; el verificador de tipos no puede inferirlo a través del dict.
    return cast(Runnable[dict, M], con_reintentos)
