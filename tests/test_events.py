"""Eventos de progreso en el Redis Stream de cada job."""
from app.queue import events


async def test_publicar_y_leer_en_orden(r):
    await events.publicar(r, "j1", "estado", status="RUNNING")
    await events.publicar(r, "j1", "nodo", nodo="supervisor")

    leidos = await events.leer(r, "j1", desde="0", bloqueo_ms=10)

    assert [(tipo, datos) for _, tipo, datos in leidos] == [
        ("estado", {"status": "RUNNING"}),
        ("nodo", {"nodo": "supervisor"}),
    ]
    assert await r.ttl("eventos:j1") > 0


async def test_leer_desde_un_id_entrega_solo_lo_posterior(r):
    await events.publicar(r, "j1", "nodo", nodo="supervisor")
    await events.publicar(r, "j1", "nodo", nodo="investigador")
    primero, *_ = await events.leer(r, "j1", desde="0", bloqueo_ms=10)

    siguientes = await events.leer(r, "j1", desde=primero[0], bloqueo_ms=10)

    assert [datos["nodo"] for _, _, datos in siguientes] == ["investigador"]


async def test_sin_eventos_devuelve_lista_vacia(r):
    assert await events.leer(r, "nadie", desde="0", bloqueo_ms=10) == []


def test_estados_que_cierran_el_stream():
    assert events.es_final("estado", {"status": "DONE"})
    assert events.es_final("estado", {"status": "WAITING_APPROVAL"})
    assert not events.es_final("estado", {"status": "RUNNING"})
    assert not events.es_final("nodo", {"nodo": "sintesis"})
