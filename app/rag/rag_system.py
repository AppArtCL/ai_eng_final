"""Sistema RAG híbrido: BM25 (léxico) + Pinecone (semántico) con EnsembleRetriever, y re-ranking.

Uso:
    uv run python -m app.rag.rag_system   # requiere OPENAI_API_KEY y PINECONE_API_KEY en .env

Salida de ejemplo SIN re-ranking (pesos 0.5/0.5, obtenida en la Pre-entrega 4):

    Consulta: ¿Puedo tener mascota?
      1. convivencia_privada-001    | Tenencia y Regulación de Animales Domésticos o Mascotas
      2. asados-001                 | Regulación Exhaustiva sobre Asados y Tipo de Parrillas
      3. obras_mudanzas-005         | Gastos Comunes, Prorrateo Centralizado y Corte de Suministro
      4. convivencia_privada-003    | Normas de Convivencia, Emisiones de Humo y Ruido
      5. obras_mudanzas-004         | Régimen de Estacionamientos de Visitas y Circulaciones Viales

    Consulta: ¿Qué dice el reglamento sobre las UTM?
      1. convivencia_privada-004    | Procedimiento Sancionatorio y Régimen de Multas
      2. convivencia_privada-002    | Destino Exclusivo Habitacional y Prohibición Comercial
      3. convivencia_privada-000    | Introducción
      4. asados-003                 | Régimen de Terrazas, Balcones y Preservación de Fachadas
      5. convivencia_privada-001    | Tenencia y Regulación de Animales Domésticos o Mascotas

    Consulta: Departamentos 101 y 104:
      1. obras_mudanzas-001         | Normas sobre Reformas Interiores y Prohibición de Modificar Pisos
      2. asados-004                 | Polígonos de Uso y Goce Exclusivo en Cubiertas y Jardines
      3. convivencia_privada-002    | Destino Exclusivo Habitacional y Prohibición Comercial
      4. asados-002                 | Prevención de Incendios y Materiales Inflamables
      5. asados-001                 | Regulación Exhaustiva sobre Asados y Tipo de Parrillas

    Consulta: ¿Puedo hacer asados?
      1. convivencia_privada-003    | Normas de Convivencia, Emisiones de Humo y Ruido
      2. asados-001                 | Regulación Exhaustiva sobre Asados y Tipo de Parrillas
      3. obras_mudanzas-005         | Gastos Comunes, Prorrateo Centralizado y Corte de Suministro
      4. asados-000                 | Introducción
      5. obras_mudanzas-004         | Régimen de Estacionamientos de Visitas y Circulaciones Viales

Notas sobre la salida:
    - "mascota" y "UTM" aciertan en 1.er lugar gracias a preprocesar() (retrievers.py).
    - "101 y 104" es ambigua: ambos chunks del top-2 mencionan esos departamentos.
    - En "asados", BM25 da a "hacer" el mismo peso que a "asado" (ambas aparecen en un solo chunk),
      por lo que "Humo y Ruido" sube al 1.er lugar; el chunk correcto queda 2.º.
"""
import logging

from langchain_classic.retrievers import ContextualCompressionRetriever, EnsembleRetriever
from langchain_core.documents import BaseDocumentCompressor, Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_pinecone import PineconeRerank, PineconeVectorStore

from app import config
from app.rag.ingest import cargar_chunks
from app.rag.retrievers import crear_bm25, crear_vectorial

logger = logging.getLogger(__name__)


def crear_reranker(top_n: int) -> PineconeRerank:
    """Re-ranker hospedado en Pinecone: usa la misma API key que el índice."""
    return PineconeRerank(
        model=config.settings.rerank_model,
        top_n=top_n,
        pinecone_api_key=config.settings.pinecone_api_key,
    )


class RAGSystem:
    """Búsqueda híbrida (BM25 + Pinecone) en dos etapas: recuperar muchos candidatos y re-rankear.

    1. Cada retriever trae `candidatos` fragmentos y EnsembleRetriever los fusiona con RRF.
    2. Un cross-encoder (re-ranker) puntúa cada par (pregunta, fragmento) y deja los `k` mejores.

    RRF solo combina posiciones; el re-ranker lee la pregunta y el fragmento juntos, así que
    corrige casos como "¿Puedo hacer asados?", donde BM25 premiaba la palabra "hacer".
    Si el re-ranking falla (límite de la API, red), with_fallbacks entrega los k primeros del híbrido.
    """

    # bm25, vectorial y reranker se pueden inyectar ya construidos (tests offline, evaluación).
    def __init__(
        self,
        k: int = config.settings.rag_top_k,
        candidatos: int = config.settings.rag_candidatos,
        pesos: tuple[float, float] = (config.settings.rag_peso_bm25, 1 - config.settings.rag_peso_bm25),
        bm25: BaseRetriever | None = None,
        vectorial: BaseRetriever | None = None,
        reranker: BaseDocumentCompressor | None = None,
    ):
        self.k = k
        self.pesos = pesos
        self.bm25 = bm25 or crear_bm25(cargar_chunks(), candidatos)
        self.vectorial = vectorial or crear_vectorial(candidatos)
        self.reranker = reranker
        self.retriever_hibrido = EnsembleRetriever(
            retrievers=[self.bm25, self.vectorial],
            weights=list(pesos),
            id_key="chunk_id",
        )
        solo_hibrido = self.retriever_hibrido | RunnableLambda(self._primeros_k, name="top_k_hibrido")
        self.busqueda: Runnable[str, list[Document]]
        if reranker is None:
            self.busqueda = solo_hibrido
        else:
            con_rerank = ContextualCompressionRetriever(
                base_compressor=reranker, base_retriever=self.retriever_hibrido
            )
            self.busqueda = con_rerank.with_fallbacks([solo_hibrido])

    @classmethod
    def desde_config(cls) -> "RAGSystem":
        """El sistema completo según la configuración (con re-ranking si RERANK_ACTIVO)."""
        k = config.settings.rag_top_k
        return cls(k=k, reranker=crear_reranker(k) if config.settings.rerank_activo else None)

    def _primeros_k(self, documentos: list[Document]) -> list[Document]:
        return documentos[: self.k]

    async def aretrieve(self, query: str) -> list[Document]:
        """Los k fragmentos más relevantes. Async: embeddings, Pinecone y re-rank no bloquean el loop."""
        return await self.busqueda.ainvoke(query)

    def retrieve(self, query: str) -> list[Document]:
        """Versión síncrona, para scripts de línea de comandos y la evaluación offline."""
        return self.busqueda.invoke(query)

    def _vectorstore_pinecone(self) -> PineconeVectorStore | None:
        vectorstore = getattr(self.vectorial, "vectorstore", None)
        return vectorstore if isinstance(vectorstore, PineconeVectorStore) else None

    async def aabrir(self) -> None:
        """Abre una sola sesión HTTP async con Pinecone, compartida por todas las búsquedas.

        Necesario con búsquedas concurrentes: si no, langchain-pinecone abre y CIERRA su cliente
        compartido en cada consulta, y la primera que termina cierra la sesión que las demás
        todavía usan ("RuntimeError: Session is closed"). Con el vectorstore abierto, las consultas
        reutilizan la sesión (y sus conexiones) y nadie la cierra hasta acerrar().
        """
        if (vectorstore := self._vectorstore_pinecone()) is not None:
            await vectorstore.__aenter__()

    async def acerrar(self) -> None:
        """Cierra las sesiones HTTP async: la del índice y la del re-ranker (que no se cierra sola)."""
        if (vectorstore := self._vectorstore_pinecone()) is not None:
            await vectorstore.aclose()
        cliente = getattr(self.reranker, "async_client", None)
        if cliente is not None:
            await cliente.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    rag = RAGSystem.desde_config()
    consultas = [
        "¿Puedo tener mascota?",
        "¿Qué dice el reglamento sobre las UTM?",
        "Departamentos 101 y 104:",
        "¿Puedo hacer asados?"
    ]
    for consulta in consultas:
        print(f"\nConsulta: {consulta}")
        for posicion, doc in enumerate(rag.retrieve(consulta), start=1):
            score = doc.metadata.get("relevance_score")
            relevancia = f"{score:.3f}" if score is not None else "  -  "
            print(f"  {posicion}. {doc.metadata['chunk_id']:<26} | {relevancia} | {doc.metadata['seccion']}")
