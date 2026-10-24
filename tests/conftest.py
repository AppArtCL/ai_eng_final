"""Configuración común de los tests.

Los agentes crean sus LLM al importarse y `app.config.Settings` exige claves no vacías
al importar `app`. Se definen claves falsas antes de importar `app`: construir los
clientes no llama a la red, así que los tests no necesitan credenciales reales.
"""
import asyncio
import os
import uuid

import fakeredis
import pytest
import redis

os.environ.setdefault("OPENAI_API_KEY", "sk-test-falsa")
os.environ.setdefault("PINECONE_API_KEY", "pc-test-falsa")
os.environ.setdefault("PINECONE_INDEX_NAME", "edificio-riesco")

REDIS_URL_TEST = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


def _redis_disponible() -> bool:
    try:
        return bool(redis.Redis.from_url(REDIS_URL_TEST, socket_connect_timeout=0.5).ping())
    except redis.exceptions.RedisError:
        return False


def pytest_collection_modifyitems(config, items):
    """Salta los tests marcados con @pytest.mark.redis si no hay un Redis levantado."""
    if _redis_disponible():
        return
    saltar = pytest.mark.skip(reason=f"Redis no disponible en {REDIS_URL_TEST} (docker compose up -d redis)")
    for item in items:
        if "redis" in item.keywords:
            item.add_marker(saltar)


class FakeRedisBloqueante(fakeredis.aioredis.FakeRedis):
    """FakeRedis cuyo BLMOVE espera el timeout cuando no hay mensajes, como Redis real.

    fakeredis 2.40 devuelve None al instante si la lista de origen existió y quedó vacía, lo que
    haría girar en vacío el bucle del worker (Redis real sí bloquea, igual que con BRPOP).
    """

    async def blmove(self, first_list, second_list, timeout, src="LEFT", dest="RIGHT"):
        resultado = await super().blmove(first_list, second_list, timeout, src=src, dest=dest)
        if resultado is None:
            await asyncio.sleep(timeout)
        return resultado


@pytest.fixture
def r() -> FakeRedisBloqueante:
    return FakeRedisBloqueante(decode_responses=True)


@pytest.fixture
def redis_url() -> str:
    return REDIS_URL_TEST


@pytest.fixture
def prefijo_unico() -> str:
    """Prefijo de thread_id por test, para no chocar con datos de corridas anteriores."""
    return f"test-{uuid.uuid4().hex[:8]}"
