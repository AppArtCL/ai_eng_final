"""Contratos de la API: lo que entra y sale de cada endpoint, validado con Pydantic."""
from pydantic import BaseModel, Field

from app.queue.jobs import Estado


class NuevaTarea(BaseModel):
    pregunta: str = Field(min_length=1, max_length=2000, description="Pregunta o solicitud del vecino")


class TareaEncolada(BaseModel):
    job_id: str
    status: Estado


class EstadoTarea(BaseModel):
    job_id: str
    status: Estado
    pregunta: str
    respuesta: str | None
    error: str | None
    aprobacion_pendiente: dict | None = Field(description="Acción que espera aprobación (solo en WAITING_APPROVAL)")
    created_at: str
    updated_at: str


class DecisionHumana(BaseModel):
    aprobado: bool
    comentario: str = Field(default="", max_length=500)
