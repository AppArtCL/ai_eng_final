"""Pruebas de punta a punta contra la pila real, con evidencia de observabilidad.

Corre 5 escenarios (API → Redis → workers → grafo → LLM/Pinecone), sigue cada uno en vivo por SSE,
verifica el resultado, consulta en Phoenix el costo, la latencia y el desglose por nodo de cada
traza, y captura el árbol de spans con Chrome headless.

Uso (con `docker compose up` corriendo; el escenario 5 reinicia el contenedor de la API):
    uv run python scripts/e2e.py
    uv run python scripts/e2e.py --sin-capturas      # sin Chrome
    uv run python scripts/e2e.py --sin-reinicio      # sin matar la API (por ejemplo, fuera de Docker)

Salida: docs/evidencia/pruebas-e2e.md, docs/evidencia/pruebas-e2e.json y docs/evidencia/capturas/.
"""
import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parent.parent
EVIDENCIA = RAIZ / "docs" / "evidencia"
CAPTURAS = EVIDENCIA / "capturas"
NODOS = ("supervisor", "investigador", "analista", "aprobacion_humana", "gestor", "sintesis")
CHROME_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
# La API exporta los spans a Phoenix por lotes (BatchSpanProcessor, cada ~5 s). Un SIGKILL pierde lo
# que esté en el buffer: antes de matar la API se espera a que salga el lote de los escenarios previos.
ESPERA_LOTE_SPANS_S = 8


@dataclass
class Escenario:
    nombre: str
    que_prueba: str
    pregunta: str
    decision: bool | None = None          # aprobar / rechazar si el grafo pide aprobación
    comentario: str = ""
    reiniciar: bool = False               # matar la API (SIGKILL) a mitad de la ejecución
    nodos_esperados: tuple[str, ...] = ()  # nodos que deben aparecer
    nodos_prohibidos: tuple[str, ...] = () # nodos que no deben aparecer


ESCENARIOS = [
    Escenario(
        "consulta-normativa", "RAG híbrido + re-ranking, citas de la fuente y streaming de la respuesta",
        "¿Puedo hacer asados en mi terraza? ¿Qué tipo de parrilla se permite?",
        nodos_esperados=("supervisor", "investigador", "sintesis"), nodos_prohibidos=("gestor",),
    ),
    Escenario(
        "calculo-multipaso", "Delegación investigador → analista: el supervisor no calcula, el analista usa la calculadora",
        "Si hago ruidos molestos después de las 22:00 en tres ocasiones distintas este mes, ¿cuánto pagaría en total de multa?",
        nodos_esperados=("investigador", "analista", "sintesis"),
    ),
    Escenario(
        "hitl-aprobado", "Acción con efectos reales: pausa con interrupt(), aprobación humana y registro idempotente",
        "Quiero registrar una mudanza para el sábado 10 de enero a las 10:00, departamento 1203.",
        decision=True, comentario="Visto bueno de la administración",
        nodos_esperados=("aprobacion_humana", "gestor", "sintesis"),
    ),
    Escenario(
        "hitl-rechazado", "Rechazo humano: el gestor no se ejecuta y la respuesta explica el motivo",
        "Quiero registrar una mudanza para el sábado 17 de enero a las 09:30, departamento 502.",
        decision=False, comentario="Ese sábado hay mantención de ascensores",
        nodos_esperados=("aprobacion_humana", "sintesis"), nodos_prohibidos=("gestor",),
    ),
    Escenario(
        "reinicio-a-mitad", "Persistencia: SIGKILL a la API a mitad del grafo; el job se retoma desde su checkpoint en Redis",
        "¿Cuántas horas a la semana en total se permiten mudanzas en el edificio, sumando todos los días?",
        reiniciar=True, nodos_esperados=("investigador", "sintesis"),
    ),
]


@dataclass
class Resultado:
    escenario: str
    job_id: str
    status: str = ""
    respuesta: str = ""
    nodos: list[str] = field(default_factory=list)  # en el orden en que llegaron por SSE
    tokens_streaming: int = 0
    reconexiones_sse: int = 0
    duracion_s: float = 0.0
    verificaciones: dict[str, bool] = field(default_factory=dict)
    trazas: list[dict] = field(default_factory=list)
    capturas: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.verificaciones) and all(self.verificaciones.values())


# --- API: seguimiento en vivo por SSE ---------------------------------------------------

async def seguir_eventos(api: httpx.AsyncClient, resultado: Resultado, desde: str | None) -> tuple[str | None, str]:
    """Lee el stream SSE del job hasta un estado final. Devuelve (último id, estado)."""
    headers = {"Last-Event-ID": desde} if desde else {}
    ultimo, estado, evento = desde, "", {}
    async with api.stream("GET", f"/tasks/{resultado.job_id}/events", headers=headers, timeout=None) as resp:
        resp.raise_for_status()
        async for linea in resp.aiter_lines():
            if linea.startswith(("id:", "event:", "data:")):
                clave, valor = linea.split(":", 1)
                evento[clave] = valor.strip()
            elif not linea and "data" in evento:
                ultimo, datos = evento.get("id", ultimo), json.loads(evento["data"])
                if evento.get("event") == "nodo":
                    resultado.nodos.append(datos["nodo"])
                elif evento.get("event") == "token":
                    resultado.tokens_streaming += 1
                elif evento.get("event") == "estado":
                    estado = datos["status"]
                evento = {}
    return ultimo, estado


async def esperar_api(api: httpx.AsyncClient, segundos: int = 90) -> None:
    for _ in range(segundos):
        try:
            if (await api.get("/health")).status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(1)
    raise RuntimeError("La API no volvió a responder después del reinicio")


async def docker_compose(*args: str) -> None:
    proceso = await asyncio.create_subprocess_exec("docker", "compose", *args, cwd=RAIZ,
                                                   stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    if await proceso.wait() != 0:
        raise RuntimeError(f"Falló: docker compose {' '.join(args)}")


async def reiniciar_api_cuando_avance(resultado: Resultado) -> None:
    """Espera a que el grafo complete al menos un nodo y mata la API (SIGKILL, sin apagado ordenado)."""
    while not resultado.nodos:
        await asyncio.sleep(0.1)
    await docker_compose("kill", "api")
    await docker_compose("up", "-d", "api")


async def correr(api: httpx.AsyncClient, escenario: Escenario, permitir_reinicio: bool) -> Resultado:
    inicio = time.perf_counter()
    respuesta = await api.post("/tasks", json={"pregunta": escenario.pregunta})
    respuesta.raise_for_status()
    resultado = Resultado(escenario.nombre, respuesta.json()["job_id"])

    reinicio = asyncio.create_task(reiniciar_api_cuando_avance(resultado)) \
        if escenario.reiniciar and permitir_reinicio else None
    ultimo, estado = None, ""
    while estado not in ("DONE", "FAILED"):
        try:
            ultimo, estado = await seguir_eventos(api, resultado, ultimo)
        except (httpx.TransportError, httpx.HTTPStatusError):
            # La API murió con el stream abierto: se espera que vuelva y se retoma desde el último evento.
            resultado.reconexiones_sse += 1
            await esperar_api(api)
            continue
        if estado == "WAITING_APPROVAL":
            if escenario.decision is None:
                raise RuntimeError(f"{escenario.nombre}: pidió aprobación sin que el escenario lo esperara")
            (await api.post(f"/tasks/{resultado.job_id}/approve",
                            json={"aprobado": escenario.decision, "comentario": escenario.comentario})).raise_for_status()
    if reinicio:
        await reinicio

    job = (await api.get(f"/tasks/{resultado.job_id}")).json()
    resultado.status, resultado.respuesta = job["status"], job.get("respuesta") or job.get("error") or ""
    resultado.duracion_s = time.perf_counter() - inicio
    resultado.verificaciones = {
        "terminó en DONE": resultado.status == "DONE",
        "recibió tokens por SSE": resultado.tokens_streaming > 0,
        **{f"pasó por {n}": n in resultado.nodos for n in escenario.nodos_esperados},
        **{f"no pasó por {n}": n not in resultado.nodos for n in escenario.nodos_prohibidos},
    }
    if escenario.reiniciar and permitir_reinicio:
        resultado.verificaciones["el stream se cortó y se reanudó con Last-Event-ID"] = resultado.reconexiones_sse > 0
    return resultado


# --- Phoenix: costo, latencia y desglose por nodo ---------------------------------------

CONSULTA_TRAZA = """
query($proyecto: ID!, $traza: ID!) { node(id: $proyecto) { ... on Project { trace(traceId: $traza) {
  latencyMs
  costSummary { total { cost tokens } }
  spans(first: 1000) { edges { node {
    spanId parentId name spanKind latencyMs costSummary { total { cost tokens } } } } }
} } } }"""


async def id_proyecto(phoenix: httpx.AsyncClient, nombre: str) -> str:
    datos = (await phoenix.post("/graphql", json={"query": "{ projects { edges { node { id name } } } }"})).json()
    return next(e["node"]["id"] for e in datos["data"]["projects"]["edges"] if e["node"]["name"] == nombre)


async def raices_de_jobs(phoenix: httpx.AsyncClient, proyecto: str, desde: str) -> list[dict]:
    """Spans raíz "job" creados desde el inicio de la corrida (con paginación)."""
    raices, cursor = [], None
    while True:
        params = {"name": "job", "start_time": desde, "limit": 100, **({"cursor": cursor} if cursor else {})}
        datos = (await phoenix.get(f"/v1/projects/{proyecto}/spans", params=params)).json()
        raices += datos["data"]
        if not (cursor := datos.get("next_cursor")):
            return raices


def desglosar(spans: list[dict]) -> dict[str, dict]:
    """Por nodo del grafo: latencia y suma de tokens y costo de las llamadas al LLM que cuelgan de él."""
    hijos: dict[str | None, list[dict]] = {}
    for s in spans:
        hijos.setdefault(s["parentId"], []).append(s)

    def llms_bajo(span: dict) -> list[dict]:
        pendientes, encontrados = [span], []
        while pendientes:
            actual = pendientes.pop()
            if actual["spanKind"] == "llm":
                encontrados.append(actual)
            pendientes += hijos.get(actual["spanId"], [])
        return encontrados

    por_nodo: dict[str, dict] = {}
    for s in spans:
        if s["name"] not in NODOS:
            continue
        llms = llms_bajo(s)
        acumulado = por_nodo.setdefault(s["name"], {"ejecuciones": 0, "latencia_ms": 0.0, "tokens": 0, "costo_usd": 0.0})
        acumulado["ejecuciones"] += 1
        acumulado["latencia_ms"] += s["latencyMs"] or 0
        acumulado["tokens"] += int(sum((l["costSummary"] or {}).get("total", {}).get("tokens") or 0 for l in llms))
        acumulado["costo_usd"] += sum((l["costSummary"] or {}).get("total", {}).get("cost") or 0 for l in llms)
    return por_nodo


async def trazas_del_job(phoenix: httpx.AsyncClient, proyecto_id: str, raices: list[dict], job_id: str) -> list[dict]:
    trazas = []
    for raiz in sorted((r for r in raices if r["attributes"].get("job.id") == job_id), key=lambda r: r["start_time"]):
        variables = {"proyecto": proyecto_id, "traza": raiz["context"]["trace_id"]}
        datos = (await phoenix.post("/graphql", json={"query": CONSULTA_TRAZA, "variables": variables})).json()
        traza = datos["data"]["node"]["trace"]
        total = (traza["costSummary"] or {}).get("total") or {}
        trazas.append({
            "trace_id": raiz["context"]["trace_id"],
            "tipo": raiz["attributes"].get("job.tipo"),
            "retomado": bool(raiz["attributes"].get("job.retomado")),
            "latencia_s": round(traza["latencyMs"] / 1000, 2),
            "tokens": int(total.get("tokens") or 0),
            "costo_usd": total.get("cost") or 0.0,
            "nodos": desglosar([e["node"] for e in traza["spans"]["edges"]]),
        })
    return trazas


# --- Capturas con Chrome headless -------------------------------------------------------

def ruta_chrome() -> str | None:
    candidatos = [os.environ.get("CHROME_PATH"), CHROME_MAC, shutil.which("google-chrome"), shutil.which("chromium")]
    return next((c for c in candidatos if c and Path(c).exists()), None)


async def capturar(chrome: str, url: str, destino: Path, alto: int = 1400) -> bool:
    with tempfile.TemporaryDirectory() as perfil:
        proceso = await asyncio.create_subprocess_exec(
            chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size=1600,{alto}",
            "--virtual-time-budget=15000", f"--user-data-dir={perfil}", f"--screenshot={destino}", url,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proceso.wait(), timeout=90)
    return destino.exists() and destino.stat().st_size > 10_000


# --- Reporte ---------------------------------------------------------------------------

def usd(valor: float) -> str:
    return f"{valor:.4f}"


def reporte(resultados: list[Resultado], escenarios: list[Escenario], fecha: str) -> str:
    total_costo = sum(t["costo_usd"] for r in resultados for t in r.trazas)
    lineas = [
        "# Pruebas de punta a punta con trazas",
        "",
        f"Corrida del {fecha} con `uv run python scripts/e2e.py` contra `docker compose up` "
        "(API + Redis + Phoenix, OpenAI y Pinecone reales). Cada escenario se siguió en vivo por SSE; "
        "costo, latencia y desglose por nodo salen de la API GraphQL de Phoenix.",
        "",
        "| # | Escenario | Qué prueba | Estado | Nodos (en orden) | Trazas | Latencia | Tokens | Costo (USD) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, (r, e) in enumerate(zip(resultados, escenarios), start=1):
        latencia = sum(t["latencia_s"] for t in r.trazas)
        tokens = sum(t["tokens"] for t in r.trazas)
        costo = sum(t["costo_usd"] for t in r.trazas)
        marca = "✅" if r.ok else "❌"
        lineas.append(f"| {i} | `{r.escenario}` | {e.que_prueba} | {marca} {r.status} | {' → '.join(r.nodos)} "
                      f"| {len(r.trazas)} | {latencia:.1f} s | {tokens:,} | {usd(costo)} |")
    lineas += ["", f"**Costo total de la corrida: USD {usd(total_costo)}.**", ""]

    agregado: dict[str, dict] = {}
    for r in resultados:
        for t in r.trazas:
            for nodo, d in t["nodos"].items():
                a = agregado.setdefault(nodo, {"ejecuciones": 0, "latencia_ms": 0.0, "tokens": 0, "costo_usd": 0.0})
                for clave in a:
                    a[clave] += d[clave]
    lineas += [
        "## Costo y latencia por nodo (todas las pruebas)", "",
        "| Nodo | Ejecuciones | Latencia promedio | Latencia total | Tokens | Costo (USD) | % del costo |",
        "|---|---|---|---|---|---|---|",
    ]
    for nodo in sorted(agregado, key=lambda n: -agregado[n]["costo_usd"]):
        a = agregado[nodo]
        porcentaje = a["costo_usd"] / total_costo if total_costo else 0
        lineas.append(f"| {nodo} | {a['ejecuciones']} | {a['latencia_ms'] / a['ejecuciones'] / 1000:.1f} s "
                      f"| {a['latencia_ms'] / 1000:.1f} s | {a['tokens']:,} | {usd(a['costo_usd'])} | {porcentaje:.0%} |")

    lineas += ["", "## Detalle por escenario", ""]
    for i, (r, e) in enumerate(zip(resultados, escenarios), start=1):
        lineas += [f"### {i}. `{r.escenario}`", "", f"**Pregunta:** {e.pregunta}", "",
                   f"`job_id`: `{r.job_id}` · duración vista por el cliente: {r.duracion_s:.1f} s · "
                   f"tokens recibidos por SSE: {r.tokens_streaming}"
                   + (f" · reconexiones SSE: {r.reconexiones_sse}" if r.reconexiones_sse else ""), "",
                   "Verificaciones: " + " · ".join(f"{'✅' if v else '❌'} {k}" for k, v in r.verificaciones.items()), ""]
        for t in r.trazas:
            etiqueta = "reanudación tras aprobar" if t["tipo"] == "resume" else "inicio"
            if t["retomado"]:
                etiqueta += ", retomada desde el checkpoint tras el reinicio"
            lineas.append(f"- Traza `{t['trace_id']}` ({etiqueta}): {t['latencia_s']} s, {t['tokens']:,} tokens, "
                          f"USD {usd(t['costo_usd'])}")
        for captura in r.capturas:
            lineas += ["", f"![{r.escenario}](capturas/{captura})"]
        respuesta = r.respuesta.replace("\n", " ")
        lineas += ["", f"> {respuesta[:600]}{'…' if len(respuesta) > 600 else ''}", ""]
    return "\n".join(lineas)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--phoenix", default="http://localhost:6006")
    parser.add_argument("--proyecto", default=os.environ.get("PHOENIX_PROJECT", "riesco-api"))
    parser.add_argument("--sin-capturas", action="store_true")
    parser.add_argument("--sin-reinicio", action="store_true")
    args = parser.parse_args()

    inicio = datetime.now(timezone.utc)
    resultados = []
    async with httpx.AsyncClient(base_url=args.api, timeout=30) as api:
        await esperar_api(api, segundos=10)
        for i, escenario in enumerate(ESCENARIOS, start=1):
            if escenario.reiniciar and not args.sin_reinicio:
                await asyncio.sleep(ESPERA_LOTE_SPANS_S)
            print(f"[{i}/{len(ESCENARIOS)}] {escenario.nombre} …", flush=True)
            resultado = await correr(api, escenario, permitir_reinicio=not args.sin_reinicio)
            resultados.append(resultado)
            print(f"      {'✅' if resultado.ok else '❌'} {resultado.status} en {resultado.duracion_s:.1f} s · "
                  f"{' → '.join(resultado.nodos)}", flush=True)

    print("Esperando a que Phoenix reciba los últimos spans …", flush=True)
    await asyncio.sleep(10)
    async with httpx.AsyncClient(base_url=args.phoenix, timeout=60) as phoenix:
        proyecto_id = await id_proyecto(phoenix, args.proyecto)
        raices = await raices_de_jobs(phoenix, args.proyecto, inicio.isoformat())
        for resultado, escenario in zip(resultados, ESCENARIOS):
            resultado.trazas = await trazas_del_job(phoenix, proyecto_id, raices, resultado.job_id)
            # Un job con aprobación produce dos trazas (pausa y reanudación); los demás, una. Con el
            # reinicio, la traza de la vida que murió se pierde (SIGKILL) y queda la retomada.
            esperadas = 2 if escenario.decision is not None else 1
            resultado.verificaciones[f"Phoenix registró sus {esperadas} traza(s)"] = len(resultado.trazas) == esperadas
            if escenario.reiniciar and not args.sin_reinicio:
                resultado.verificaciones["la traza quedó marcada job.retomado"] = any(t["retomado"] for t in resultado.trazas)

    chrome = None if args.sin_capturas else ruta_chrome()
    if chrome:
        CAPTURAS.mkdir(parents=True, exist_ok=True)
        base = f"{args.phoenix}/projects/{proyecto_id}"
        if await capturar(chrome, f"{base}/traces", CAPTURAS / "00-lista-de-trazas.png", alto=1000):
            print("Captura: 00-lista-de-trazas.png")
        for i, resultado in enumerate(resultados, start=1):
            for j, traza in enumerate(resultado.trazas, start=1):
                nombre = f"{i:02d}-{resultado.escenario}" + (f"-{j}" if len(resultado.trazas) > 1 else "") + ".png"
                if await capturar(chrome, f"{base}/traces/{traza['trace_id']}", CAPTURAS / nombre):
                    resultado.capturas.append(nombre)
                    print(f"Captura: {nombre}")
    elif not args.sin_capturas:
        print("No se encontró Chrome: sin capturas (define CHROME_PATH para usar otro navegador).")

    EVIDENCIA.mkdir(parents=True, exist_ok=True)
    fecha = inicio.astimezone().strftime("%d-%m-%Y %H:%M")
    (EVIDENCIA / "pruebas-e2e.md").write_text(reporte(resultados, ESCENARIOS, fecha), encoding="utf-8")
    (EVIDENCIA / "pruebas-e2e.json").write_text(
        json.dumps([asdict(r) | {"ok": r.ok} for r in resultados], ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nReporte: {(EVIDENCIA / 'pruebas-e2e.md').relative_to(RAIZ)}")
    fallidos = [r.escenario for r in resultados if not r.ok]
    print("✅ Los 5 escenarios pasaron." if not fallidos else f"❌ Fallaron: {', '.join(fallidos)}")
    return 1 if fallidos else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
