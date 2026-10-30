# ADR 0002 · API asíncrona con cola en Redis y workers en el mismo proceso

**Estado:** aceptada · **Fecha:** 2026-10

## Contexto

Una ejecución del grafo tarda entre 8 y 20 s (varias llamadas al LLM y a Pinecone) y puede quedar
**pausada indefinidamente** esperando una aprobación humana. Un endpoint que ejecute el grafo dentro
de la petición HTTP sufre timeouts de clientes y proxies, y no tiene cómo representar "pausado".

## Decisión

- `POST /tasks` solo registra el job en Redis y lo encola: responde **202 con el `job_id` en milisegundos** (p50 entre 6 y 15 ms en las corridas de `scripts/carga.py`).
- Un pool de **workers asyncio dentro del proceso de la API** (lanzados en el `lifespan`) consume la
  cola y ejecuta el grafo. El cliente sigue el progreso por polling (`GET /tasks/{id}`) o en vivo
  por SSE (ADR 0010).
- Todo el código es `async` de punta a punta: nodos `async def` con `ainvoke`, `redis.asyncio`,
  búsquedas de Pinecone y re-ranking asíncronos. Lo único síncrono es CPU pura (BM25, calculadora),
  y el RAG se construye en un thread al arrancar (`asyncio.to_thread`).

## Alternativas consideradas

| Alternativa | Por qué no |
|---|---|
| Ejecutar el grafo dentro del request | Timeouts; no representa la pausa del HITL |
| Celery / Arq / RQ en un proceso aparte | Un servicio más que operar; el volumen de este sistema no lo requiere. Como la cola ya vive en Redis, migrar es cambiar quién consume `cola:tareas`, no la API |
| Nodos síncronos ejecutados por LangGraph en un threadpool | No bloquea el loop, pero ocupa un thread por job esperando red; no es "async de punta a punta" |

## Consecuencias

- ➕ Mientras un job espera al LLM, el mismo event loop atiende la API y los demás workers. Medido
  con `uv run python scripts/carga.py`, que sondea `/health` cada 100 ms mientras corren 5 jobs:
  **p50 ≈ 3,5 ms, p95 ≈ 5 ms** (en reposo, ~6 ms). Si algo bloqueara el loop, se dispararía.
- ➕ Concurrencia acotada y explícita (`NUM_WORKERS`).
- ➖ Las réplicas de la API comparten la cola pero no la recuperación de huérfanos (ver ADR 0004).
- ➖ La concurrencia real expuso un bug de `langchain-pinecone`: su cliente async compartido se
  cerraba al terminar cada búsqueda (`RuntimeError: Session is closed`, 3 de 5 jobs fallaban). Se
  resolvió abriendo la sesión una vez en el `lifespan` (`RAGSystem.aabrir`); hay test de regresión.
