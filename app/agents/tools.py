"""Herramientas de los agentes, con contratos Pydantic de entrada y salida (ver app/agents/schemas.py).

Cada herramienta devuelve (texto para el LLM, artefacto validado para el código).
"""
import ast
import json
import operator as op
from datetime import datetime, timezone
from functools import lru_cache
from typing import cast

from langchain_core.documents import Document
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from redis.asyncio import Redis
from redis.exceptions import WatchError

from app import config
from app.agents.schemas import (
    BusquedaReglamento,
    Calculo,
    Fragmento,
    ResultadoBusqueda,
    ResultadoCalculo,
    SolicitudNueva,
    SolicitudRegistrada,
    TipoSolicitud,
)
from app.rag.rag_system import RAGSystem

CLAVE_SOLICITUDES = "solicitudes:administracion"

_OPERADORES = {
    ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
    ast.FloorDiv: op.floordiv, ast.Mod: op.mod, ast.Pow: op.pow,
    ast.USub: op.neg, ast.UAdd: op.pos,
}
_MAX_EXPONENTE = 100


# --- buscar_en_reglamento -----------------------------------------------------------

@lru_cache(maxsize=1)
def get_rag() -> RAGSystem:
    """Construye el RAGSystem una sola vez y lo reutiliza en cada búsqueda.

    Construirlo lee los documentos y arma el índice BM25 en memoria (CPU, síncrono): la API lo
    precalienta en un thread al arrancar, para que la primera búsqueda no bloquee el event loop.
    """
    return RAGSystem.desde_config()


def _fragmento(documento: Document) -> Fragmento:
    return Fragmento(
        chunk_id=documento.metadata.get("chunk_id", "?"),
        fuente=documento.metadata.get("source", "?"),
        seccion=documento.metadata.get("seccion", "?"),
        contenido=documento.page_content,
        relevancia=documento.metadata.get("relevance_score"),
    )


def _citar(fragmento: Fragmento) -> str:
    """Texto que ve el LLM: la fuente primero, para que la cite; la relevancia, para que pondere."""
    relevancia = f" | Relevancia: {fragmento.relevancia:.2f}" if fragmento.relevancia is not None else ""
    return f"[Fuente: {fragmento.fuente} | Sección: {fragmento.seccion}{relevancia}]\n{fragmento.contenido}"


@tool(args_schema=BusquedaReglamento, response_format="content_and_artifact")
async def buscar_en_reglamento(pregunta: str) -> tuple[str, ResultadoBusqueda]:
    """Busca información en el Reglamento de Copropiedad del Edificio Riesco 6540.

    Úsala para cualquier pregunta sobre normas, prohibiciones, horarios, montos o
    plazos del edificio: mascotas, ruidos, asados y parrillas, piscina, salón multiuso,
    mudanzas, obras, estacionamientos, multas (UTM) y gastos comunes.
    """
    resultado = ResultadoBusqueda(
        pregunta=pregunta,
        fragmentos=[_fragmento(d) for d in await get_rag().aretrieve(pregunta)],
    )
    if not resultado.fragmentos:
        return "No se encontró información relevante en el reglamento.", resultado
    return "\n\n".join(_citar(f) for f in resultado.fragmentos), resultado


# --- calculadora --------------------------------------------------------------------

def _evaluar_nodo(nodo: ast.AST) -> float:
    """Recorre el árbol de la expresión y evalúa solo nodos permitidos."""
    if isinstance(nodo, ast.Constant) and type(nodo.value) in (int, float):
        return cast(float, nodo.value)  # el type() de arriba ya garantizó que es un número
    if isinstance(nodo, ast.BinOp) and type(nodo.op) in _OPERADORES:
        izquierda, derecha = _evaluar_nodo(nodo.left), _evaluar_nodo(nodo.right)
        if isinstance(nodo.op, ast.Pow) and abs(derecha) > _MAX_EXPONENTE:
            raise ValueError(f"exponente mayor a {_MAX_EXPONENTE}")
        return _OPERADORES[type(nodo.op)](izquierda, derecha)
    if isinstance(nodo, ast.UnaryOp) and type(nodo.op) in _OPERADORES:
        return _OPERADORES[type(nodo.op)](_evaluar_nodo(nodo.operand))
    raise ValueError(f"operación no permitida: {type(nodo).__name__}")


@tool(args_schema=Calculo, response_format="content_and_artifact")
def calculadora(expresion: str) -> tuple[str, ResultadoCalculo]:
    """Evalúa una expresión aritmética y devuelve el resultado exacto.

    Úsala para cualquier cálculo en vez de hacerlo mentalmente.
    Es síncrona a propósito: es CPU pura y no espera red.
    """
    try:
        resultado = _evaluar_nodo(ast.parse(expresion, mode="eval").body)
    except ZeroDivisionError:
        error = "división por cero."
    except (ValueError, SyntaxError) as e:
        error = str(e)
    else:
        return f"{expresion} = {resultado}", ResultadoCalculo(expresion=expresion, resultado=resultado)
    return f"Error al calcular '{expresion}': {error}", ResultadoCalculo(expresion=expresion, error=error)


# --- registrar_solicitud ------------------------------------------------------------

_redis: Redis | None = None


def configurar_redis(cliente: Redis) -> None:
    """La API entrega su cliente redis.asyncio al arrancar: un solo pool de conexiones para todo."""
    global _redis
    _redis = cliente


def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis no está configurado: la API lo hace en su lifespan (configurar_redis).")
    return _redis


def _clave_idempotencia(job_id: str, tipo: TipoSolicitud) -> str:
    return f"solicitudes:por_job:{job_id}:{tipo}"


async def guardar_solicitud(r: Redis, nueva: SolicitudNueva, job_id: str | None) -> SolicitudRegistrada:
    """Registra la solicitud una sola vez por (job, tipo), aunque el gestor se ejecute de nuevo.

    El gestor puede volver a correr si el nodo se reintenta (RetryPolicy) o si la API se reinició
    a mitad de la ejecución (el job se retoma desde el último checkpoint, que es anterior al gestor).
    La clave de idempotencia guarda la solicitud original: si ya existe, se devuelve esa, sin crear
    otra. WATCH + MULTI hace atómico "revisar la clave y, si no existe, agregar y guardar la clave".
    Sin job_id (fuera del grafo) no hay idempotencia posible y se registra siempre.
    """
    registro = {"tipo": nueva.tipo, "detalle": nueva.detalle,
                "creado_en": datetime.now(timezone.utc).isoformat(), "job_id": job_id}
    crudo = json.dumps(registro, ensure_ascii=False)  # lo que queda en la bandeja de la administración
    if job_id is None:
        numero = await r.rpush(CLAVE_SOLICITUDES, crudo)
        return SolicitudRegistrada.model_validate({**registro, "numero": numero})

    clave = _clave_idempotencia(job_id, nueva.tipo)
    ttl_seg = config.settings.checkpoint_ttl_min * 60  # después de eso el job ya no se puede retomar
    async with r.pipeline(transaction=True) as pipe:
        while True:
            try:
                await pipe.watch(clave)
                previa = cast(str | None, await pipe.get(clave))  # decode_responses=True: str
                if previa is not None:
                    await pipe.unwatch()
                    # El número es la posición del registro original en la bandeja (LPOS, base 0).
                    posicion = cast(int | None, await r.lpos(CLAVE_SOLICITUDES, previa))  # sin COUNT: un int
                    return SolicitudRegistrada.model_validate(
                        {**json.loads(previa), "numero": (posicion or 0) + 1, "repetida": True})
                pipe.multi()
                pipe.rpush(CLAVE_SOLICITUDES, crudo)
                pipe.set(clave, crudo, ex=ttl_seg)
                numero, _ = await pipe.execute()
                return SolicitudRegistrada.model_validate({**registro, "numero": numero})
            except WatchError:
                continue  # otra ejecución tocó la clave: se vuelve a revisar


@tool(args_schema=SolicitudNueva, response_format="content_and_artifact")
async def registrar_solicitud(
    tipo: TipoSolicitud, detalle: str, run_config: RunnableConfig
) -> tuple[str, SolicitudRegistrada]:
    """Registra una solicitud formal ante la administración del Edificio Riesco 6540.

    Tiene efectos reales: la solicitud queda en la bandeja de la administración.
    Úsala solo para la acción que se te indicó, una sola vez.
    """
    # run_config lo inyecta LangChain por su tipo (el LLM no lo ve): su thread_id es el job_id.
    job_id = run_config.get("configurable", {}).get("thread_id")
    solicitud = await guardar_solicitud(get_redis(), SolicitudNueva(tipo=tipo, detalle=detalle), job_id)
    if solicitud.repetida:
        texto = f"La solicitud de {tipo} ya estaba registrada con el n.º {solicitud.numero}: {solicitud.detalle}"
    else:
        texto = f"Solicitud de {tipo} registrada con el n.º {solicitud.numero}: {detalle}"
    return texto, solicitud
