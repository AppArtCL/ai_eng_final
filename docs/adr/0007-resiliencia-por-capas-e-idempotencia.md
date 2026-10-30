# ADR 0007 · Resiliencia por capas e idempotencia de los efectos laterales

**Estado:** aceptada · **Fecha:** 2026-10

## Contexto

Cada ejecución depende de servicios externos (OpenAI, Pinecone, Redis) que fallan de forma
transitoria, y de un LLM que a veces responde mal. Además, un nodo puede ejecutarse dos veces (por un
reintento o tras un reinicio, ADR 0004), y el gestor tiene efectos reales.

## Decisión

Cada capa atiende lo que solo ella puede resolver, con **un único criterio de "error transitorio"**
(el `default_retry_on` de LangGraph: red, 5xx y errores de los SDK sí; `ValueError`, `TypeError` y
otros errores de lógica no, porque fallarían igual):

| Capa | Mecanismo | Qué cubre |
|---|---|---|
| Cliente | `ChatOpenAI(max_retries=3, timeout=60)` | Red, 429 y 5xx con backoff |
| Cadena | `with_retry` solo para `RespuestaInvalida` | Salida truncada o fuera del esquema |
| Cadena / agente | `with_fallbacks` / `ModelFallbackMiddleware` | Modelo principal caído → `OPENAI_FALLBACK_MODEL` |
| Herramienta | `ToolRetryMiddleware(on_failure="continue")` | Reintenta y, si sigue fallando, **le entrega el error al agente** en vez de romper el grafo |
| Nodo | `RetryPolicy(max_attempts=2)` | Reintenta el nodo completo (no a la aprobación humana) |
| Job | `asyncio.timeout(JOB_TIMEOUT_SEG)` | Nada queda en `RUNNING` para siempre |
| RAG | `with_fallbacks` del re-ranker | Sin re-ranking, entrega el híbrido |

**Idempotencia:** `registrar_solicitud` se registra una sola vez por `(job_id, tipo)`. El `job_id`
llega a la tool por `RunnableConfig` (el LLM no lo ve). `WATCH` + `MULTI` hace atómico "revisar la
clave y, si no existe, registrar"; si existe, se devuelve la solicitud original (`repetida=True`).

## Alternativas consideradas

- **Reintentar todo en todas las capas:** multiplica la latencia (3 × 3 × 3) y reintenta bugs.
  La configuración por defecto de `ToolRetryMiddleware` reintentaba cualquier excepción; se ajustó.
- **Idempotencia por el texto del detalle:** al reintentar, el LLM redacta distinto → duplicados.

## Consecuencias

- ➕ Tests: 5 reintentos concurrentes del mismo job dejan **un** registro; una tool caída produce un
  `ToolMessage` de error y el agente responde "no pude consultar"; el modelo de respaldo toma el
  control si el principal falla.
- ➕ Verificado con la pila real: `SIGKILL` justo después del registro → el job se retoma y queda
  **una** solicitud.
- ➖ La idempotencia por `(job, tipo)` impide dos solicitudes del mismo tipo en un mismo job (por
  ejemplo, dos reclamos distintos). Hoy el flujo pide una acción por aprobación.
