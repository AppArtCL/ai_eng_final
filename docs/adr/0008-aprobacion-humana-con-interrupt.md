# ADR 0008 · Aprobación humana con `interrupt()` antes de acciones con efectos

**Estado:** aceptada · **Fecha:** 2026-10

## Contexto

El gestor registra solicitudes ante la administración: un efecto real que no se puede deshacer
desde el sistema. Un LLM puede decidir mal (registrar algo que el vecino solo preguntaba), así que la
acción necesita un humano en el ciclo, sin bloquear un worker mientras espera.

## Decisión

- **Qué es crítico se decide en código**, no en el prompt: `es_critica()` en
  [`app/graph/hitl.py`](../../app/graph/hitl.py) marca como crítica toda decisión que vaya a un
  agente de `AGENTES_CON_EFECTOS` (hoy, el gestor). El gestor no tiene arista directa desde el
  supervisor: solo se llega a él pasando por `aprobacion_humana`.
- El nodo `aprobacion_humana` llama a `interrupt()` con la acción, la razón y la pregunta. LangGraph
  guarda el checkpoint y el worker queda libre; el job pasa a `WAITING_APPROVAL` con
  `aprobacion_pendiente` tipada (`AprobacionPendiente`).
- `POST /tasks/{id}/approve` encola la reanudación (`Command(resume=...)`), con `WATCH`/`MULTI` para
  que un doble clic no reanude dos veces (el segundo recibe 409).
- Un rechazo queda como aporte ("Acción rechazada por el humano…"): el supervisor no la reintenta y
  la síntesis le explica el motivo al vecino.

## Alternativas consideradas

| Alternativa | Por qué no |
|---|---|
| `interrupt_before=["gestor"]` al compilar | Funciona, pero la política queda implícita en la compilación; un nodo explícito documenta qué se pregunta y con qué datos |
| Que el gestor pida confirmación en el chat | El LLM decide cuándo preguntar: un falso negativo ejecuta sin permiso |
| Esperar la aprobación dentro del worker | Bloquea un worker por cada job pausado |

## Consecuencias

- ➕ Una pausa no consume recursos: puede durar horas y sobrevive a reinicios (ADR 0003).
- ➕ En Phoenix, pausa y reanudación son dos trazas de la misma sesión (`session.id = job_id`).
- ➖ `/approve` no tiene autenticación: en producción debería exigir un rol de administración.
