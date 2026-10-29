"""Contratos Pydantic de las herramientas de los agentes.

Entrada (args_schema): lo que el LLM puede enviar. Si los argumentos no cumplen el contrato, el
ToolNode de create_agent le devuelve el error de validación al modelo, que corrige y reintenta.

Salida (artefacto): las herramientas usan response_format="content_and_artifact". El LLM recibe
un texto legible (content) y el código recibe el objeto validado (ToolMessage.artifact), sin
tener que interpretar el texto. Por ejemplo, el gestor verifica que hubo un registro mirando el
artefacto SolicitudRegistrada, no buscando palabras en la respuesta del modelo.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, PositiveInt

TipoSolicitud = Literal["mudanza", "reserva", "reclamo"]


# --- buscar_en_reglamento -----------------------------------------------------------

class BusquedaReglamento(BaseModel):
    pregunta: str = Field(
        min_length=3, max_length=500,
        description="Términos o pregunta a buscar en el reglamento, por ejemplo: 'horario permitido para mudanzas'",
    )


class Fragmento(BaseModel):
    """Un fragmento del reglamento con su fuente: lo que permite citar en la respuesta final."""
    chunk_id: str
    fuente: str = Field(description="Archivo del reglamento, por ejemplo obras_mudanzas.txt")
    seccion: str
    contenido: str
    relevancia: float | None = Field(default=None, ge=0, le=1, description="Puntaje del re-ranker, si lo hubo")


class ResultadoBusqueda(BaseModel):
    pregunta: str
    fragmentos: list[Fragmento]


# --- calculadora --------------------------------------------------------------------

class Calculo(BaseModel):
    expresion: str = Field(
        min_length=1, max_length=200,
        # Solo números, operadores y paréntesis: "5 UTM * 2" se rechaza antes de evaluar,
        # y el error le dice al modelo qué corregir.
        pattern=r"^[0-9\s+\-*/%().]+$",
        description="Expresión aritmética con números, + - * / // % ** y paréntesis. "
                    "Sin unidades ni variables, decimales con punto. Ejemplo: '5 * (17.5 - 7) + (14 - 9)'",
    )


class ResultadoCalculo(BaseModel):
    expresion: str
    resultado: float | None = None
    error: str | None = None


# --- registrar_solicitud ------------------------------------------------------------

class SolicitudNueva(BaseModel):
    tipo: TipoSolicitud = Field(description='"mudanza", "reserva" (quincho, salón multiuso) o "reclamo"')
    detalle: str = Field(
        min_length=10, max_length=500,
        description="Fecha, hora, departamento y todo lo necesario para gestionarla. "
                    "Ejemplo: 'sábado 10:00 a 14:00, depto 101'",
    )


class SolicitudRegistrada(BaseModel):
    numero: PositiveInt = Field(description="Número correlativo en la bandeja de la administración")
    tipo: TipoSolicitud
    detalle: str
    creado_en: datetime
    job_id: str | None = Field(default=None, description="Ejecución que la registró (clave de idempotencia)")
    repetida: bool = Field(default=False, description="True si ya estaba registrada y no se volvió a crear")
