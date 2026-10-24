"""La configuración falla al arrancar, con un mensaje claro, si el entorno es inválido."""
import pytest
from pydantic import ValidationError

from app.config import Settings

MINIMO = {"OPENAI_API_KEY": "sk-x", "PINECONE_API_KEY": "pc-x", "PINECONE_INDEX_NAME": "indice-ok"}


@pytest.fixture
def entorno(monkeypatch):
    """Entorno controlado: sin .env y solo con las variables que defina cada test."""
    for campo in Settings.model_fields:
        monkeypatch.delenv(campo.upper(), raising=False)

    def definir(**variables: str) -> Settings:
        for nombre, valor in {**MINIMO, **variables}.items():
            monkeypatch.setenv(nombre, valor)
        return Settings(_env_file=None)  # type: ignore[call-arg]

    return definir


def test_valores_por_defecto(entorno):
    s = entorno()
    assert s.embedding_model == "text-embedding-3-small"
    assert s.num_workers == 5
    assert s.checkpoint_ttl_min == 1440


def test_la_clave_no_se_filtra_al_imprimir(entorno):
    s = entorno(OPENAI_API_KEY="sk-secreta")
    assert "sk-secreta" not in repr(s)
    assert s.openai_api_key.get_secret_value() == "sk-secreta"


@pytest.mark.parametrize(
    "variables",
    [
        {"OPENAI_API_KEY": ""},  # clave vacía, como al copiar .env.example sin completarlo
        {"PINECONE_INDEX_NAME": "Indice_Malo"},  # Pinecone solo acepta minúsculas, números y guiones
        {"REDIS_URL": "http://localhost:6379"},
        {"RAG_PESO_BM25": "1.5"},
        {"CHUNK_SIZE": "100", "CHUNK_OVERLAP": "100"},
    ],
)
def test_entorno_invalido_falla_al_arrancar(entorno, variables):
    with pytest.raises(ValidationError):
        entorno(**variables)
