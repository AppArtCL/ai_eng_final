"""Lectura y fragmentación del reglamento (app/rag/data/*.txt) y subida a Pinecone.

El punto de entrada es app/rag/preparar.py, que decide si hace falta subir.
"""
import os
import logging
from app import config
import time
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_pinecone import PineconeVectorStore

logger = logging.getLogger(__name__)

# Determina si una línea es un título.
def es_titulo(linea: str) -> bool:    
    return (
        0 < len(linea) < 110
        and not linea[0].isspace()
        and not linea.startswith("⚬")
        and not linea.rstrip().endswith((".", ":"))
    )

# Lee y genera secciones de un documento.
def parsear_documento(ruta: str) -> list[Document]:
    with open(ruta, encoding="utf-8") as f:
        lineas = f.read().strip().splitlines()
    
    source = os.path.basename(ruta)
    doc_id = source.removesuffix(".txt")
    titulo = lineas[0].strip()

    secciones = []
    seccion_actual = "Introducción"
    buffer = []
    
    for linea in lineas[1:]:
        if es_titulo(linea):
            if buffer:
                secciones.append((seccion_actual, "\n".join(buffer)))
            seccion_actual = linea.strip()
            buffer = []
        else:
            buffer.append(linea)

    if buffer:
        secciones.append((seccion_actual, "\n".join(buffer)))
    
    return [
        Document(
            page_content=f"{seccion}\n{texto}",
            metadata={
                "doc_id": doc_id,
                "source": source,
                "titulo": titulo,
                "seccion": seccion,
            }
        )
        for seccion, texto in secciones
    ]

def cargar_chunks():
    secciones = []
    for ruta in sorted(config.DATA_DIR.glob("*.txt")):
        secciones.extend(parsear_documento(str(ruta)))

    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name=config.settings.encoding_name,
        chunk_size=config.settings.chunk_size,
        chunk_overlap=config.settings.chunk_overlap
    )

    chunks = splitter.split_documents(secciones)
    if not chunks:
        logger.error(f"No se encontraron documentos en {config.DATA_DIR}.")
        raise ValueError(f"No se encontraron documentos en {config.DATA_DIR}.")
    
    contadores = {}
    for chunk in chunks:
        doc_id = chunk.metadata.get("doc_id")
        i = contadores.get(doc_id, 0)
        chunk.metadata["chunk_index"] = i
        chunk.metadata["chunk_id"] = f"{doc_id}-{i:03d}"
        contadores[doc_id] = i + 1

    return chunks

# Espero hasta que el conteo sea exacto o se espere 30 segundos.
def esperar_conteo(index, esperado, timeout=30):
    inicio = time.time()
    n = 0
    while time.time() - inicio < timeout:
        stats = index.describe_index_stats()
        n = stats["namespaces"].get(config.settings.pinecone_namespace, {}).get("vector_count", 0)
        if n >= esperado:
            return n
        time.sleep(2)
    logger.error(f"Timeout alcanzado. Se esperaban {esperado} vectores, pero solo se contaron {n}.")
    return n

def subir_a_pinecone(chunks):
    vectorstore = PineconeVectorStore.from_documents(
        documents=chunks,
        embedding=config.get_embeddings(),
        pinecone_api_key=config.settings.pinecone_api_key.get_secret_value(),
        index_name=config.settings.pinecone_index_name,
        namespace=config.settings.pinecone_namespace,
        ids=[c.metadata["chunk_id"] for c in chunks],
    )
    
    n = esperar_conteo(vectorstore.index, esperado=len(chunks))
    
    logger.info(f"Vectores en el namespace {config.settings.pinecone_namespace}: {n}.")
