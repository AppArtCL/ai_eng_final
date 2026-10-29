"""Evaluación de la recuperación: búsqueda híbrida sola contra híbrida + re-ranking.

Uso (requiere las claves en .env; hace llamadas reales a OpenAI y Pinecone):
    uv run python -m app.rag.evaluate

Se mide por SECCIÓN, no por documento: cada documento tiene varias secciones y lo que el agente
necesita es el fragmento correcto. Y se mide el ORDEN, porque eso es lo que cambia el re-ranker:
  - Acierto@1: la sección correcta quedó en primer lugar.
  - MRR (Mean Reciprocal Rank): promedio de 1/posición de la sección correcta (0 si no aparece).
  - Recall@k: la sección correcta aparece entre los k fragmentos que recibe el agente.
"""
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from langchain_core.documents import Document

from app import config
from app.rag.ingest import cargar_chunks
from app.rag.rag_system import RAGSystem, crear_reranker
from app.rag.retrievers import crear_bm25, crear_vectorial

GOLDEN_SET = Path(__file__).parent / "golden_set.json"
SALIDA = config.BASE_DIR / "docs" / "evidencia" / "evaluacion-rag.md"


@dataclass
class Resultado:
    id: int
    tipo: str
    pregunta: str
    esperada: str
    posicion: int | None  # 1 = primer lugar; None = no aparece en el top k
    secciones: list[str]


def posicion_de(secciones: list[str], esperada: str) -> int | None:
    """Posición (desde 1) de la primera aparición de la sección esperada, o None."""
    return next((i for i, s in enumerate(secciones, start=1) if s == esperada), None)


def metricas(resultados: list[Resultado]) -> dict[str, float]:
    n = len(resultados)
    return {
        "acierto@1": sum(r.posicion == 1 for r in resultados) / n,
        "mrr": sum(1 / r.posicion for r in resultados if r.posicion) / n,
        "recall@k": sum(r.posicion is not None for r in resultados) / n,
    }


async def evaluar(sistema: RAGSystem, golden_set: list[dict]) -> list[Resultado]:
    resultados = []
    for caso in golden_set:
        documentos: list[Document] = await sistema.aretrieve(caso["pregunta"])
        secciones = [d.metadata.get("seccion", "?") for d in documentos]
        resultados.append(Resultado(caso["id"], caso["tipo"], caso["pregunta"], caso["seccion_esperada"],
                                    posicion_de(secciones, caso["seccion_esperada"]), secciones))
    return resultados


def formatear_reporte(por_sistema: dict[str, list[Resultado]], k: int) -> str:
    nombres = list(por_sistema)
    lineas = [
        "# Evaluación de la recuperación: híbrido vs. híbrido + re-ranking",
        "",
        f"Golden set de {len(por_sistema[nombres[0]])} preguntas (`app/rag/golden_set.json`), "
        f"k = {k} fragmentos, re-ranker `{config.settings.rerank_model}` (Pinecone). "
        "Generado con `uv run python -m app.rag.evaluate`.",
        "",
        "| Sistema | Acierto@1 | MRR | Recall@" + str(k) + " |",
        "|---|---|---|---|",
    ]
    for nombre, resultados in por_sistema.items():
        m = metricas(resultados)
        lineas.append(f"| {nombre} | {m['acierto@1']:.0%} | {m['mrr']:.2f} | {m['recall@k']:.0%} |")
    lineas += ["", "## Detalle por pregunta (posición de la sección correcta)", "",
               "| # | Tipo | Pregunta | " + " | ".join(nombres) + " |",
               "|---|---|---|" + "---|" * len(nombres)]
    for i, base in enumerate(por_sistema[nombres[0]]):
        posiciones = []
        for nombre in nombres:
            p = por_sistema[nombre][i].posicion
            posiciones.append(f"{p}.º" if p else "—")
        lineas.append(f"| {base.id} | {base.tipo} | {base.pregunta} | " + " | ".join(posiciones) + " |")
    return "\n".join(lineas) + "\n"


async def main() -> None:
    golden_set = json.loads(GOLDEN_SET.read_text(encoding="utf-8"))
    k, candidatos = config.settings.rag_top_k, config.settings.rag_candidatos
    # Los mismos retrievers para ambos sistemas: la única diferencia es el re-ranker.
    bm25, vectorial = crear_bm25(cargar_chunks(), candidatos), crear_vectorial(candidatos)
    sistemas = {
        "Híbrido (BM25 + Pinecone, RRF)": RAGSystem(k=k, bm25=bm25, vectorial=vectorial),
        "Híbrido + re-ranking": RAGSystem(k=k, bm25=bm25, vectorial=vectorial, reranker=crear_reranker(k)),
    }
    por_sistema = {}
    for nombre, sistema in sistemas.items():
        await sistema.aabrir()
        try:
            por_sistema[nombre] = await evaluar(sistema, golden_set)
        finally:
            await sistema.acerrar()

    reporte = formatear_reporte(por_sistema, k)
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text(reporte, encoding="utf-8")
    print(reporte)
    print(f"Reporte guardado en {SALIDA.relative_to(config.BASE_DIR)}")


if __name__ == "__main__":
    asyncio.run(main())
