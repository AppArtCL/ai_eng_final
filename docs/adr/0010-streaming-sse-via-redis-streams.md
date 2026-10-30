# ADR 0010 · Progreso en vivo con SSE alimentado por Redis Streams

**Estado:** aceptada · **Fecha:** 2026-10

## Contexto

Con polling, el vecino espera 10 s o más sin feedback. El M1 trabajó el streaming de respuestas,
pero aquí el grafo no corre en la petición HTTP sino en un worker (ADR 0002): el streaming no puede
ser simplemente "devolver el iterador del LLM".

## Decisión

```
worker ── grafo.astream(stream_mode=["updates", "messages"]) ──XADD──▶ eventos:{job_id}
API ─── GET /tasks/{id}/events ──XREAD──▶ Server-Sent Events ──▶ cliente
```

- El worker ejecuta el grafo con `astream` en vez de `ainvoke` y publica: cambios de **estado**,
  **nodos** terminados y los **tokens** de la síntesis (solo de ese nodo; los de los agentes internos
  son pasos intermedios).
- El endpoint usa el SSE nativo de FastAPI (`EventSourceResponse`) y lee el stream con `XREAD`
  bloqueante. Cada evento lleva su id: el cliente se reconecta con `Last-Event-ID` sin perder nada.
- El stream se cierra en `DONE`, `FAILED` o `WAITING_APPROVAL`. Los eventos expiran una hora después.
- Estado del job y evento se escriben juntos (`_cambiar_estado`): polling y SSE nunca se contradicen.

## Alternativas consideradas

| Alternativa | Por qué no |
|---|---|
| Redis Pub/Sub | No guarda historia: un cliente que se conecta tarde o se reconecta pierde eventos |
| WebSockets | Bidireccional, innecesario aquí; SSE funciona sobre HTTP simple y reconecta solo |
| Polling más frecuente | Más carga y aun así sin tokens en vivo |

## Consecuencias

- ➕ En las pruebas de punta a punta el cliente recibe más de 100 tokens por respuesta, y el
  escenario de reinicio verifica la reconexión con `Last-Event-ID` tras el `SIGKILL`.
- ➖ Los tokens son un **borrador**: si la validación de cifras de la síntesis pide reescribir, la
  respuesta definitiva es la del evento `DONE`.
- ➖ La validación de que el job existe va en una dependencia (`Depends`): dentro del generador SSE
  ya no se puede responder 404 (la respuesta empezó). Lo detectó un test.
