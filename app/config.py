"""Configuración única del sistema, validada con Pydantic al arrancar.

Todos los valores salen de variables de entorno (o del .env en desarrollo local). Si falta una
credencial o un valor es inválido, el proceso falla al importar este módulo con un mensaje que
indica qué variable corregir, en vez de fallar a mitad de una petición.
"""
from pathlib import Path
from typing import Annotated, Literal

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from pydantic import Field, PositiveInt, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # raíz del repo (.env, diagrama)
DATA_DIR = BASE_DIR / "app" / "rag" / "data"

Secreto = Annotated[SecretStr, Field(min_length=1)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",  # el .env puede traer variables de otras herramientas
    )

    # Credenciales
    openai_api_key: Secreto
    pinecone_api_key: Secreto

    # Modelos
    openai_model: str = "gpt-4.1-mini"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: PositiveInt = 1536

    # Pinecone
    pinecone_index_name: str = Field(pattern=r"^[a-z0-9-]{1,45}$")
    pinecone_namespace: str = "consultas-convivencia"
    pinecone_metric: Literal["cosine", "euclidean", "dotproduct"] = "cosine"
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"

    # RAG
    chunk_size: PositiveInt = 600
    chunk_overlap: Annotated[int, Field(ge=0)] = 100
    encoding_name: str = "cl100k_base"
    rag_top_k: PositiveInt = 5
    rag_peso_bm25: Annotated[float, Field(ge=0, le=1)] = 0.5  # el vectorial recibe 1 - este peso

    # Grafo
    max_pasos: PositiveInt = 6

    # Redis: estado de los jobs, cola de tareas y checkpoints de LangGraph
    redis_url: str = Field("redis://localhost:6379/0", pattern=r"^rediss?://")
    num_workers: PositiveInt = 5
    checkpoint_ttl_min: PositiveInt = 1440  # los checkpoints de un hilo expiran tras 24 h sin uso

    # Observabilidad: trazas OpenInference hacia Arize Phoenix. Vacío desactiva el envío.
    phoenix_collector_endpoint: str = "http://localhost:6006/v1/traces"
    phoenix_project: str = "riesco-api"

    @model_validator(mode="after")
    def _overlap_menor_que_chunk(self) -> "Settings":
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"CHUNK_OVERLAP ({self.chunk_overlap}) debe ser menor que CHUNK_SIZE ({self.chunk_size})."
            )
        return self


settings = Settings()  # type: ignore[call-arg]  # los campos obligatorios vienen del entorno


def get_embeddings() -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=settings.openai_api_key,
        dimensions=settings.embedding_dim,
    )


def get_llm() -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        temperature=0,
        max_retries=3,
    )
