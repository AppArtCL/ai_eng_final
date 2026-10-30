# ADR 0003 · Redis como estado de jobs, cola, eventos y checkpointer de LangGraph

**Estado:** aceptada · **Fecha:** 2026-10

## Contexto

El sistema necesita persistir cuatro cosas: el estado de cada job (para el polling), la cola de
trabajo, los eventos de progreso (para SSE) y los **checkpoints del grafo**, que permiten pausar en
el HITL y retomar tras un reinicio. La consigna exige que las conversaciones sobrevivan al
reinicio del servidor, lo que descarta `MemorySaver`.

## Decisión

Un solo Redis 8 (incluye RedisJSON y RediSearch, que requiere `langgraph-checkpoint-redis`):

| Uso | Estructura |
|---|---|
| Estado del job | Hash `job:{id}` |
| Cola | Listas `cola:tareas` → `cola:procesando` (ADR 0004) |
| Progreso en vivo | Stream `eventos:{id}` (ADR 0010) |
| Checkpoints | `AsyncRedisSaver`, `thread_id = job_id`, TTL configurable (`CHECKPOINT_TTL_MIN`) |
| Idempotencia del gestor | `solicitudes:por_job:{id}:{tipo}` (ADR 0007) |

Datos en un volumen de Docker: sobreviven a `docker compose down` y a reinicios de la API.

## Alternativas consideradas

| Alternativa | Por qué no |
|---|---|
| `AsyncSqliteSaver` (usado en el M5) | Un archivo local no se comparte entre réplicas ni entre contenedores |
| Postgres (`AsyncPostgresSaver`) | Más robusto para auditoría, pero sería un segundo almacén solo para checkpoints, cuando Redis ya hace falta para la cola |

## Consecuencias

- ➕ Una sola dependencia de infraestructura para todo el estado.
- ➕ Probado con la pila real: un job pausado sobrevive a `docker compose restart api`, y uno a mitad
  de ejecución se retoma tras un `SIGKILL` ([README](../../README.md#persistencia-y-recuperación)).
- ➖ Los checkpoints expiran (`CHECKPOINT_TTL_MIN`, 24 h por defecto): un job no aprobado en ese
  plazo ya no se puede retomar. Es intencional: acota la memoria.
- ➖ Redis como almacén principal exige persistencia configurada (RDB/AOF) en producción.
- ⚠️ Al restaurar un checkpoint, los artefactos de las tools vuelven como `dict` serializado de
  LangChain, no como el objeto Pydantic original: se re-validan (ADR 0006).
