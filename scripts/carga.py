"""Prueba de carga: 5 peticiones concurrentes contra la API y seguimiento hasta que terminan.

Lanza los POST /tasks al mismo tiempo, hace polling de cada job y muestra una tabla con el
tiempo de cada uno, más p50/p95 del lado del cliente. El costo por ejecución y la latencia
p95 "oficiales" son los que calcula Phoenix (http://localhost:6006); estos números sirven
para contrastarlos.

Uso (con la API levantada):
    uv run python scripts/carga.py
    uv run python scripts/carga.py --con-hitl        # suma una solicitud crítica y la aprueba
    uv run python scripts/carga.py --url http://otra-maquina:8000
"""
import argparse
import asyncio
import statistics
import sys
import time

import httpx

PREGUNTAS = [
    "¿Cuántas horas a la semana se permiten mudanzas en el edificio?",
    "¿Puedo tener mascota en mi departamento? ¿Qué condiciones hay?",
    "¿Hasta qué hora se puede usar el salón multiuso y para cuántas personas como máximo?",
    "¿Se pueden hacer asados en la terraza? ¿Qué tipo de parrilla está permitida?",
    "¿Cuál es la multa por ruidos molestos y cómo se aplica?",
]
PREGUNTA_CRITICA = "Quiero registrar una solicitud de mudanza para el sábado 22 de noviembre de 10:00 a 13:00, departamento 305."

ESTADOS_FINALES = {"DONE", "FAILED"}
INTERVALO_POLLING_SEG = 1
TIMEOUT_JOB_SEG = 180


async def ejecutar(cliente: httpx.AsyncClient, pregunta: str) -> dict:
    """Encola una pregunta y espera su estado final; si pide aprobación, la aprueba."""
    inicio = time.perf_counter()
    resp = await cliente.post("/tasks", json={"pregunta": pregunta})
    resp.raise_for_status()
    job_id = resp.json()["job_id"]
    encolado_ms = (time.perf_counter() - inicio) * 1000

    status = "PENDING"
    while time.perf_counter() - inicio < TIMEOUT_JOB_SEG:
        await asyncio.sleep(INTERVALO_POLLING_SEG)
        status = (await cliente.get(f"/tasks/{job_id}")).json()["status"]
        if status == "WAITING_APPROVAL":
            await cliente.post(f"/tasks/{job_id}/approve", json={"aprobado": True, "comentario": "aprobado por scripts/carga.py"})
        elif status in ESTADOS_FINALES:
            break
    else:
        status = f"TIMEOUT ({status})"

    return {"job_id": job_id, "status": status, "pregunta": pregunta,
            "encolado_ms": encolado_ms, "total_seg": time.perf_counter() - inicio}


def percentil(valores: list[float], p: int) -> float:
    """Percentil con interpolación lineal (método 'inclusive', el mismo criterio de numpy)."""
    return statistics.quantiles(valores, n=100, method="inclusive")[p - 1]


def mostrar(resultados: list[dict]) -> None:
    print(f"\n{'job_id':<34} {'status':<10} {'POST (ms)':>9} {'total (s)':>9}  pregunta")
    for r in resultados:
        print(f"{r['job_id']:<34} {r['status']:<10} {r['encolado_ms']:>9.0f} {r['total_seg']:>9.1f}  {r['pregunta'][:60]}")
    tiempos = [r["total_seg"] for r in resultados]
    posts = [r["encolado_ms"] for r in resultados]
    print(f"\nLatencia de punta a punta (cliente): p50 = {percentil(tiempos, 50):.1f} s · p95 = {percentil(tiempos, 95):.1f} s")
    print(f"Respuesta del POST /tasks:           p50 = {percentil(posts, 50):.0f} ms · p95 = {percentil(posts, 95):.0f} ms")
    print("El tiempo del cliente incluye el intervalo de polling (±1 s); la cifra de referencia es la de Phoenix.")


async def main(url: str, con_hitl: bool) -> int:
    preguntas = PREGUNTAS + ([PREGUNTA_CRITICA] if con_hitl else [])
    async with httpx.AsyncClient(base_url=url, timeout=10) as cliente:
        try:
            (await cliente.get("/health")).raise_for_status()
        except httpx.HTTPError as e:
            print(f"❌ La API no responde en {url}: {e}")
            return 1
        print(f"🚀 Lanzando {len(preguntas)} peticiones concurrentes contra {url} ...")
        resultados = await asyncio.gather(*(ejecutar(cliente, p) for p in preguntas))

    mostrar(resultados)
    fallidos = [r for r in resultados if r["status"] != "DONE"]
    if fallidos:
        print(f"\n❌ {len(fallidos)} job(s) no terminaron en DONE.")
        return 1
    print("\n✅ Todos los jobs terminaron en DONE. Revisa las trazas en http://localhost:6006")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Prueba de carga con peticiones concurrentes.")
    parser.add_argument("--url", default="http://localhost:8000", help="URL base de la API")
    parser.add_argument("--con-hitl", action="store_true", help="agrega una solicitud que pasa por aprobación humana")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.url, args.con_hitl)))
