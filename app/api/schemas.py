"""Contratos de la API: lo que entra y sale de cada endpoint, validado con Pydantic.

Todo lo que cruza la frontera HTTP tiene un modelo: también los errores y el health check, para
que la documentación OpenAPI (/docs) describa cada respuesta posible y no solo el caso feliz.
"""
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Path
from pydantic import BaseModel, ConfigDict, Field

from app.queue.jobs import Estado

# Los job_id son uuid4().hex: 32 caracteres hexadecimales. Un id con otro formato se rechaza con
# 422 antes de consultar Redis.
JobId = Annotated[str, Path(
    pattern=r"^[0-9a-f]{32}$",
    description="Identificador del trabajo devuelto por POST /tasks",
    examples=["8b8ef9615ad44c3c9f1f2e0a7d3b5c21"],
)]


class NuevaTarea(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [
        {"pregunta": "¿Cuántas horas a la semana se permiten mudanzas en el edificio?"},
        {"pregunta": "Quiero registrar una mudanza para el sábado 22 de noviembre a las 10:00, depto 305."},
    ]})

    pregunta: str = Field(min_length=1, max_length=2000, description="Pregunta o solicitud del vecino")


class TareaEncolada(BaseModel):
    job_id: str
    status: Estado


class AprobacionPendiente(BaseModel):
    """Lo que el grafo le pregunta al humano antes de ejecutar una acción con efectos reales."""
    accion: str = Field(description="Instrucción que recibiría el gestor si se aprueba")
    razon: str = Field(description="Por qué el supervisor decidió pedir la acción")
    pregunta: str = Field(description="Pregunta original del vecino")


class EstadoTarea(BaseModel):
    job_id: str
    status: Estado
    pregunta: str
    respuesta: str | None = Field(description="Respuesta final (solo en DONE)")
    error: str | None = Field(description="Causa del fallo (solo en FAILED)")
    aprobacion_pendiente: AprobacionPendiente | None = Field(
        description="Acción que espera aprobación (solo en WAITING_APPROVAL)")
    created_at: datetime
    updated_at: datetime


class DecisionHumana(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [
        {"aprobado": True, "comentario": "Visto bueno de la administración"},
        {"aprobado": False, "comentario": "Ese sábado hay mantención de ascensores"},
    ]})

    aprobado: bool
    comentario: str = Field(default="", max_length=500)


class Salud(BaseModel):
    redis: Literal["ok"]


class ErrorRespuesta(BaseModel):
    """Forma de todas las respuestas de error de la API (la misma que usa FastAPI)."""
    detail: str
