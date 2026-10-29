import ast
import json
import operator as op
from datetime import datetime, timezone
from functools import lru_cache
from typing import Literal

from langchain_core.tools import tool
from redis.asyncio import Redis

from app.rag.rag_system import RAGSystem

CLAVE_SOLICITUDES = "solicitudes:administracion"

_OPERADORES = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv, ast.Mod: op.mod, ast.Pow: op.pow,
    ast.USub: op.neg, ast.UAdd: op.pos,
}
_MAX_EXPONENTE = 100

@lru_cache(maxsize=1)
def get_rag() -> RAGSystem:
    """Construye el RAGSystem una sola vez y lo reutiliza en cada búsqueda.

    Construirlo lee los documentos y arma el índice BM25 en memoria (CPU, síncrono): la API lo
    precalienta en un thread al arrancar, para que la primera búsqueda no bloquee el event loop.
    """
    return RAGSystem.desde_config()


@tool
async def buscar_en_reglamento(pregunta: str) -> str:
    """Busca información en el Reglamento de Copropiedad del Edificio Riesco 6540.

    Úsala para cualquier pregunta sobre normas, prohibiciones, horarios, montos o
    plazos del edificio: mascotas, ruidos, asados y parrillas, piscina, salón multiuso,
    mudanzas, obras, estacionamientos, multas (UTM) y gastos comunes.

    Ejemplo de entrada: "horario permitido para mudanzas"
    """
    documentos = await get_rag().aretrieve(pregunta)
    if not documentos:
        return "No se encontró información relevante en el reglamento."
    return "\n\n".join(
        f"[Fuente: {d.metadata.get('source', '?')} | Sección: {d.metadata.get('seccion', '?')}{_relevancia(d)}]\n{d.page_content}"
        for d in documentos
    )

def _relevancia(documento) -> str:
    """Puntaje del re-ranker (0 a 1), si lo hubo: le indica al agente cuánto confiar en el fragmento."""
    score = documento.metadata.get("relevance_score")
    return f" | Relevancia: {score:.2f}" if score is not None else ""


def _evaluar_nodo(nodo):
    """Recorre el árbol de la expresión y evalúa solo nodos permitidos."""
    if isinstance(nodo, ast.Constant) and type(nodo.value) in (int, float):
        return nodo.value
    if isinstance(nodo, ast.BinOp) and type(nodo.op) in _OPERADORES:
        izquierda, derecha = _evaluar_nodo(nodo.left), _evaluar_nodo(nodo.right)
        if isinstance(nodo.op, ast.Pow) and abs(derecha) > _MAX_EXPONENTE:
            raise ValueError(f"exponente mayor a {_MAX_EXPONENTE}")
        return _OPERADORES[type(nodo.op)](izquierda, derecha)
    if isinstance(nodo, ast.UnaryOp) and type(nodo.op) in _OPERADORES:
        return _OPERADORES[type(nodo.op)](_evaluar_nodo(nodo.operand))
    raise ValueError(f"operación no permitida: {type(nodo).__name__}")

@tool
def calculadora(expresion: str) -> str:
    """Evalúa una expresión aritmética y devuelve el resultado exacto.

    Operadores permitidos: + - * / // % ** y paréntesis. Solo números, sin variables
    ni unidades. Úsala para cualquier cálculo en vez de hacerlo mentalmente.

    Ejemplo de entrada: "5 * (17.5 - 7) + (14 - 9)"
    """
    try:
        resultado = _evaluar_nodo(ast.parse(expresion, mode="eval").body)
        return f"{expresion} = {resultado}"
    except ZeroDivisionError:
        return f"Error al calcular '{expresion}': división por cero."
    except (ValueError, SyntaxError) as e:
        return f"Error al calcular '{expresion}': {e}"


_redis: Redis | None = None


def configurar_redis(cliente: Redis) -> None:
    """La API entrega su cliente redis.asyncio al arrancar: un solo pool de conexiones para todo."""
    global _redis
    _redis = cliente


def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis no está configurado: la API lo hace en su lifespan (configurar_redis).")
    return _redis


@tool
async def registrar_solicitud(tipo: Literal["mudanza", "reserva", "reclamo"], detalle: str) -> str:
    """Registra una solicitud formal ante la administración del Edificio Riesco 6540.

    Tiene efectos reales: la solicitud queda en la bandeja de la administración.
    Úsala solo para la acción que se te indicó, una sola vez.

    tipo: "mudanza", "reserva" (quincho, salón multiuso) o "reclamo".
    detalle: fecha, hora, departamento y todo lo necesario para gestionarla.

    Ejemplo de entrada: tipo="mudanza", detalle="sábado 10:00 a 14:00, depto 101"
    """
    solicitud = {"tipo": tipo, "detalle": detalle, "creado_en": datetime.now(timezone.utc).isoformat()}
    numero = await get_redis().rpush(CLAVE_SOLICITUDES, json.dumps(solicitud, ensure_ascii=False))
    return f"Solicitud de {tipo} registrada con el n.º {numero}: {detalle}"
