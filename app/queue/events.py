"""Eventos de progreso de un job en un Redis Stream (`eventos:{job_id}`).

El grafo corre en un worker, no en la petición HTTP: el worker publica aquí lo que va pasando
(qué nodo terminó, los tokens de la respuesta final, el estado del job) y el endpoint SSE de la
API lo lee con XREAD. Un Stream, a diferencia de Pub/Sub, guarda los eventos: un cliente que se
conecta tarde (o se reconecta con Last-Event-ID) recibe también lo que ya ocurrió.
"""
import json
from typing import Literal, cast

from redis.asyncio import Redis

TipoEvento = Literal["estado", "nodo", "token"]
# Forma de la respuesta de XREAD con decode_responses=True: [(stream, [(id, {campo: valor})])]
_RespuestaXread = list[tuple[str, list[tuple[str, dict[str, str]]]]]
ESTADOS_FINALES = frozenset({"DONE", "FAILED", "WAITING_APPROVAL"})  # el job deja de avanzar solo
TTL_SEG = 3600          # los eventos de un job se borran una hora después del último
MAX_EVENTOS = 5000      # tope aproximado por job (XADD MAXLEN ~)


def _clave(job_id: str) -> str:
    return f"eventos:{job_id}"


async def publicar(r: Redis, job_id: str, tipo: TipoEvento, **datos) -> None:
    """Agrega un evento al stream del job y renueva su expiración."""
    clave = _clave(job_id)
    async with r.pipeline(transaction=False) as pipe:
        pipe.xadd(clave, {"tipo": tipo, "datos": json.dumps(datos, ensure_ascii=False)},
                  maxlen=MAX_EVENTOS, approximate=True)
        pipe.expire(clave, TTL_SEG)
        await pipe.execute()


async def leer(r: Redis, job_id: str, desde: str, bloqueo_ms: int) -> list[tuple[str, str, dict]]:
    """Eventos posteriores al id `desde` ("0" = desde el inicio); espera hasta bloqueo_ms si no hay.

    Devuelve [(id, tipo, datos), ...]. El id sirve como Last-Event-ID para reanudar la lectura.
    """
    respuesta = cast(_RespuestaXread, await r.xread({_clave(job_id): desde}, block=bloqueo_ms))
    if not respuesta:
        return []
    _, entradas = respuesta[0]
    return [(id_, campos["tipo"], json.loads(campos["datos"])) for id_, campos in entradas]


def es_final(tipo: str, datos: dict) -> bool:
    """El stream SSE se cierra cuando el job llega a un estado en que ya no avanza por sí solo."""
    return tipo == "estado" and datos.get("status") in ESTADOS_FINALES
