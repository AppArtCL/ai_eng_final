import json


from app import jobs


async def _mensajes_en_cola(r) -> list[dict]:
    return [json.loads(m) for m in await r.lrange(jobs.COLA, 0, -1)]


async def test_crear_job_queda_pending_y_encolado(r):
    job_id = await jobs.crear_job(r, "¿Puedo tener mascota?")

    job = await jobs.obtener_job(r, job_id)
    assert job["job_id"] == job_id
    assert job["status"] == "PENDING"
    assert job["pregunta"] == "¿Puedo tener mascota?"
    assert job["respuesta"] is None and job["error"] is None and job["aprobacion_pendiente"] is None
    assert job["created_at"] and job["updated_at"]
    assert await _mensajes_en_cola(r) == [{"job_id": job_id, "tipo": "start"}]


async def test_cola_es_fifo(r):
    primero = await jobs.crear_job(r, "uno")
    await jobs.crear_job(r, "dos")

    _, crudo = await r.brpop([jobs.COLA], timeout=1)
    assert json.loads(crudo)["job_id"] == primero


async def test_obtener_job_inexistente_devuelve_none(r):
    assert await jobs.obtener_job(r, "no-existe") is None


async def test_actualizar_job_serializa_dict_y_limpia_con_none(r):
    job_id = await jobs.crear_job(r, "x")

    await jobs.actualizar_job(r, job_id, status="WAITING_APPROVAL", aprobacion_pendiente={"accion": "mudanza"})
    assert (await jobs.obtener_job(r, job_id))["aprobacion_pendiente"] == {"accion": "mudanza"}

    await jobs.actualizar_job(r, job_id, aprobacion_pendiente=None)
    assert (await jobs.obtener_job(r, job_id))["aprobacion_pendiente"] is None


async def test_reanudar_solo_desde_waiting_approval(r):
    job_id = await jobs.crear_job(r, "x")
    await r.delete(jobs.COLA)
    await jobs.actualizar_job(r, job_id, status="RUNNING")

    assert await jobs.solicitar_reanudacion(r, job_id, True, "") is False
    assert await _mensajes_en_cola(r) == []
    assert (await jobs.obtener_job(r, job_id))["status"] == "RUNNING"


async def test_doble_aprobacion_encola_un_solo_resume(r):
    job_id = await jobs.crear_job(r, "x")
    await r.delete(jobs.COLA)
    await jobs.actualizar_job(r, job_id, status="WAITING_APPROVAL", aprobacion_pendiente={"accion": "a"})

    assert await jobs.solicitar_reanudacion(r, job_id, True, "ok") is True
    assert await jobs.solicitar_reanudacion(r, job_id, False, "doble clic") is False

    assert await _mensajes_en_cola(r) == [
        {"job_id": job_id, "tipo": "resume", "resume": {"aprobado": True, "comentario": "ok"}}
    ]
    job = await jobs.obtener_job(r, job_id)
    assert job["status"] == "PENDING"
    assert job["aprobacion_pendiente"] is None


async def test_reanudar_job_inexistente_devuelve_false(r):
    assert await jobs.solicitar_reanudacion(r, "no-existe", True, "") is False
    assert await _mensajes_en_cola(r) == []


# --- Cola confiable ------------------------------------------------------------

async def test_tomar_mensaje_lo_mueve_a_procesando_en_orden_fifo(r):
    primero = await jobs.crear_job(r, "uno")
    await jobs.crear_job(r, "dos")

    crudo = await jobs.tomar_mensaje(r, timeout=1)

    assert json.loads(crudo)["job_id"] == primero
    assert await r.lrange(jobs.PROCESANDO, 0, -1) == [crudo]
    assert len(await _mensajes_en_cola(r)) == 1


async def test_tomar_mensaje_con_cola_vacia_devuelve_none(r):
    assert await jobs.tomar_mensaje(r, timeout=0.1) is None


async def test_confirmar_mensaje_lo_saca_de_procesando(r):
    await jobs.crear_job(r, "x")
    crudo = await jobs.tomar_mensaje(r, timeout=1)

    await jobs.confirmar_mensaje(r, crudo)

    assert await r.llen(jobs.PROCESANDO) == 0


async def test_recuperar_huerfanos_los_procesa_antes_que_los_nuevos_y_en_su_orden(r):
    a = await jobs.crear_job(r, "a")
    b = await jobs.crear_job(r, "b")
    await jobs.tomar_mensaje(r, timeout=1)
    await jobs.tomar_mensaje(r, timeout=1)
    for job_id in (a, b):
        await jobs.actualizar_job(r, job_id, status="RUNNING")
    nuevo = await jobs.crear_job(r, "llegó durante el reinicio")

    assert await jobs.recuperar_huerfanos(r) == [b, a]
    assert await r.llen(jobs.PROCESANDO) == 0
    orden = [json.loads(await jobs.tomar_mensaje(r, timeout=1))["job_id"] for _ in range(3)]
    assert orden == [a, b, nuevo]
    assert (await jobs.obtener_job(r, a))["status"] == "PENDING"


async def test_recuperar_huerfanos_no_crea_jobs_fantasma(r):
    await r.lpush(jobs.PROCESANDO, json.dumps({"job_id": "borrado", "tipo": "start"}))

    await jobs.recuperar_huerfanos(r)

    assert await jobs.obtener_job(r, "borrado") is None
