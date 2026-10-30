"""Deja el índice de Pinecone listo para la API. Idempotente: se puede correr en cada arranque.

Uso:
    uv run python -m app.rag.preparar     # docker compose lo corre como servicio `ingest` antes de la API

1. Crea el índice serverless si no existe, o valida que su dimensión coincida con EMBEDDING_DIM.
2. Compara los vectores del namespace con los fragmentos del reglamento (app/rag/data/*.txt).
3. Si coinciden, no hace nada (no gasta embeddings). Si no, sube los fragmentos.

Los ids de los vectores son deterministas (`{documento}-{n:03d}`): volver a subir sobrescribe en
vez de duplicar. Si el namespace tiene MÁS vectores que fragmentos (el reglamento o el chunking
cambiaron y quedaron ids viejos), se vacía el namespace antes de subir, para que no queden
fragmentos obsoletos que el RAG pueda recuperar.
"""
import logging

from pinecone import Pinecone, ServerlessSpec

from app import config
from app.rag.ingest import cargar_chunks, subir_a_pinecone

logger = logging.getLogger(__name__)


def asegurar_indice(pc: Pinecone) -> None:
    """Crea el índice si falta; si existe, falla claro cuando su dimensión no calza con el modelo."""
    s = config.settings
    if s.pinecone_index_name not in pc.list_indexes().names():
        logger.info("Creando el índice %s (%s, %d dimensiones).", s.pinecone_index_name, s.pinecone_metric, s.embedding_dim)
        pc.create_index(  # espera a que el índice esté listo antes de volver
            name=s.pinecone_index_name,
            dimension=s.embedding_dim,
            metric=s.pinecone_metric,
            spec=ServerlessSpec(cloud=s.pinecone_cloud, region=s.pinecone_region),
        )
        return
    dimension = pc.describe_index(s.pinecone_index_name).dimension
    if dimension != s.embedding_dim:
        raise ValueError(
            f"El índice {s.pinecone_index_name} tiene dimensión {dimension}, pero EMBEDDING_DIM es "
            f"{s.embedding_dim}: usa otro PINECONE_INDEX_NAME o ajusta EMBEDDING_MODEL/EMBEDDING_DIM."
        )


def vectores_en_namespace(pc: Pinecone) -> int:
    estadisticas = pc.Index(config.settings.pinecone_index_name).describe_index_stats()
    return estadisticas["namespaces"].get(config.settings.pinecone_namespace, {}).get("vector_count", 0)


def preparar() -> None:
    s = config.settings
    pc = Pinecone(api_key=s.pinecone_api_key.get_secret_value())
    asegurar_indice(pc)

    chunks = cargar_chunks()
    existentes = vectores_en_namespace(pc)
    if existentes == len(chunks):
        logger.info("Índice al día: %d vectores en %s/%s. No se sube nada.",
                    existentes, s.pinecone_index_name, s.pinecone_namespace)
        return
    if existentes > len(chunks):
        logger.warning("El namespace %s tiene %d vectores y el reglamento %d fragmentos: se vacía para "
                       "eliminar fragmentos obsoletos.", s.pinecone_namespace, existentes, len(chunks))
        pc.Index(s.pinecone_index_name).delete(delete_all=True, namespace=s.pinecone_namespace)
    logger.info("Subiendo %d fragmentos a %s/%s (había %d).",
                len(chunks), s.pinecone_index_name, s.pinecone_namespace, existentes)
    subir_a_pinecone(chunks)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    for ruidoso in ("httpx", "pinecone"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)
    preparar()
