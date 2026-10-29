"""Contrato de las cadenas estructuradas: validación de la respuesta cruda y reintentos."""
from typing import Literal

import pytest
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from app.chains.structured import (
    INTENTOS,
    RespuestaInvalida,
    crear_cadena_estructurada,
    validar_respuesta_completa,
)


class Ruteo(BaseModel):
    next: Literal["investigador", "FINISH"]


def _salida(parsed=None, finish_reason="stop", parsing_error=None, clave="finish_reason") -> dict:
    """Imita la salida de with_structured_output(..., include_raw=True)."""
    raw = AIMessage(content="", response_metadata={clave: finish_reason})
    return {"raw": raw, "parsed": parsed, "parsing_error": parsing_error}


# --- validar_respuesta_completa -------------------------------------------------

def test_respuesta_valida_devuelve_el_objeto_parseado():
    ruteo = Ruteo(next="FINISH")
    assert validar_respuesta_completa(_salida(parsed=ruteo)) is ruteo


@pytest.mark.parametrize(
    "clave, razon",
    [("finish_reason", "length"), ("stop_reason", "max_tokens")],  # OpenAI y Anthropic
)
def test_respuesta_truncada_es_invalida_aunque_se_haya_parseado(clave, razon):
    with pytest.raises(RespuestaInvalida, match=razon):
        validar_respuesta_completa(_salida(parsed=Ruteo(next="FINISH"), finish_reason=razon, clave=clave))


def test_respuesta_sin_parsear_es_invalida_e_informa_el_error():
    error = ValueError("next: valor 'gerente' no permitido")
    with pytest.raises(RespuestaInvalida, match="gerente"):
        validar_respuesta_completa(_salida(parsed=None, parsing_error=error))


# --- crear_cadena_estructurada ---------------------------------------------------

class LLMGuionado:
    """LLM falso: cada llamada devuelve la siguiente salida 'include_raw' del guion."""

    def __init__(self, *salidas: dict):
        self.salidas = list(salidas)
        self.llamadas = 0

    def with_structured_output(self, esquema, include_raw=False):
        assert include_raw, "la cadena debe pedir include_raw=True para poder validar la respuesta cruda"

        def responder(_):
            salida = self.salidas[min(self.llamadas, len(self.salidas) - 1)]
            self.llamadas += 1
            return salida

        return RunnableLambda(responder)


PROMPT = ChatPromptTemplate.from_messages([("human", "{contexto}")])


def test_cadena_reintenta_una_respuesta_truncada():
    llm = LLMGuionado(_salida(parsed=None, finish_reason="length"), _salida(parsed=Ruteo(next="investigador")))

    resultado = crear_cadena_estructurada(PROMPT, Ruteo, llm=llm).invoke({"contexto": "x"})

    assert resultado == Ruteo(next="investigador")
    assert llm.llamadas == 2


def test_cadena_se_rinde_tras_los_intentos_configurados():
    llm = LLMGuionado(_salida(parsed=None, parsing_error=ValueError("json roto")))

    with pytest.raises(RespuestaInvalida):
        crear_cadena_estructurada(PROMPT, Ruteo, llm=llm).invoke({"contexto": "x"})
    assert llm.llamadas == INTENTOS


def test_cadena_no_reintenta_errores_que_no_son_del_contrato():
    class LLMSinCredenciales(LLMGuionado):
        def with_structured_output(self, esquema, include_raw=False):
            def fallar(_):
                self.llamadas += 1
                raise PermissionError("401: API key inválida")
            return RunnableLambda(fallar)

    llm = LLMSinCredenciales()
    with pytest.raises(PermissionError):
        crear_cadena_estructurada(PROMPT, Ruteo, llm=llm).invoke({"contexto": "x"})
    assert llm.llamadas == 1
