"""Búsqueda en dos etapas: híbrido (BM25 + vectorial) y re-ranking, sin red (retrievers falsos)."""
from collections.abc import Sequence

from langchain_core.documents import BaseDocumentCompressor, Document
from langchain_core.retrievers import BaseRetriever

from app.rag.rag_system import RAGSystem


def _doc(chunk_id: str) -> Document:
    return Document(page_content=f"texto {chunk_id}", metadata={"chunk_id": chunk_id})


class RetrieverFijo(BaseRetriever):
    """Devuelve siempre los mismos documentos, en el mismo orden."""
    documentos: list[Document]

    def _get_relevant_documents(self, query, *, run_manager):
        return self.documentos


class RerankInvertido(BaseDocumentCompressor):
    """Re-ranker falso: invierte el orden del híbrido y anota un puntaje, como PineconeRerank."""
    top_n: int = 2

    def compress_documents(self, documents: Sequence[Document], query, callbacks=None):
        elegidos = list(reversed(documents))[: self.top_n]
        return [Document(d.page_content, metadata={**d.metadata, "relevance_score": 0.9}) for d in elegidos]


class RerankCaido(BaseDocumentCompressor):
    """El servicio de re-ranking no responde (por ejemplo, el límite mensual del plan gratuito)."""

    def compress_documents(self, documents, query, callbacks=None):
        raise RuntimeError("429 Too Many Requests")


def _rag(reranker=None) -> RAGSystem:
    candidatos = [_doc("a"), _doc("b"), _doc("c")]
    return RAGSystem(k=2, bm25=RetrieverFijo(documentos=candidatos),
                     vectorial=RetrieverFijo(documentos=candidatos), reranker=reranker)


async def test_sin_reranker_entrega_los_k_primeros_del_hibrido():
    documentos = await _rag().aretrieve("pregunta")
    assert [d.metadata["chunk_id"] for d in documentos] == ["a", "b"]


async def test_con_reranker_manda_el_orden_del_reranker():
    documentos = await _rag(RerankInvertido()).aretrieve("pregunta")

    assert [d.metadata["chunk_id"] for d in documentos] == ["c", "b"]
    assert all(d.metadata["relevance_score"] == 0.9 for d in documentos)


async def test_si_el_reranker_falla_se_usa_el_hibrido():
    documentos = await _rag(RerankCaido()).aretrieve("pregunta")
    assert [d.metadata["chunk_id"] for d in documentos] == ["a", "b"]


def test_version_sincrona_para_scripts():
    assert [d.metadata["chunk_id"] for d in _rag(RerankInvertido()).retrieve("pregunta")] == ["c", "b"]


class VectorstoreEspia:
    """Registra el ciclo de vida de la sesión HTTP async del vectorstore."""

    def __init__(self):
        self.llamadas = []

    async def __aenter__(self):
        self.llamadas.append("abrir")
        return self

    async def aclose(self):
        self.llamadas.append("cerrar")


async def test_sesion_de_pinecone_se_abre_una_vez_y_se_cierra_al_final(monkeypatch):
    """Regresión: si cada búsqueda abre y cierra el cliente compartido, las búsquedas concurrentes
    fallan con "RuntimeError: Session is closed". La API abre la sesión al arrancar y la cierra al apagar."""
    rag = _rag()
    espia = VectorstoreEspia()
    monkeypatch.setattr(rag, "_vectorstore_pinecone", lambda: espia)

    await rag.aabrir()
    await rag.aretrieve("una")
    await rag.aretrieve("otra")
    assert espia.llamadas == ["abrir"]

    await rag.acerrar()
    assert espia.llamadas == ["abrir", "cerrar"]


# --- Métricas de la evaluación (app/rag/evaluate.py) ---------------------------------

def test_posicion_de_la_seccion_esperada():
    from app.rag.evaluate import posicion_de
    assert posicion_de(["A", "B", "B"], "B") == 2
    assert posicion_de(["A", "C"], "B") is None


def test_metricas_de_orden():
    from app.rag.evaluate import Resultado, metricas
    resultados = [Resultado(1, "t", "p", "B", posicion, []) for posicion in (1, 2, None, 4)]

    m = metricas(resultados)

    assert m["acierto@1"] == 0.25
    assert m["recall@k"] == 0.75
    assert m["mrr"] == (1 + 1 / 2 + 0 + 1 / 4) / 4


def test_golden_set_apunta_a_secciones_que_existen():
    """Si alguien renombra una sección del reglamento, el golden set no puede quedar apuntando al vacío."""
    import json
    from app.rag.evaluate import GOLDEN_SET
    from app.rag.ingest import cargar_chunks
    secciones = {c.metadata["seccion"] for c in cargar_chunks()}
    casos = json.loads(GOLDEN_SET.read_text(encoding="utf-8"))
    assert len(casos) >= 15
    assert {c["seccion_esperada"] for c in casos} <= secciones
