# ADR 0004 · Cola confiable y recuperación de jobs interrumpidos

**Estado:** aceptada · **Fecha:** 2026-10 · Reemplaza el `BRPOP` de la Pre-entrega 7

## Contexto

En la Pre-entrega 7 el worker consumía con `BRPOP`, que **saca** el mensaje antes de procesarlo. Si
la API moría a mitad de un job, el mensaje desaparecía y el job quedaba en `RUNNING` para siempre,
con el cliente haciendo polling sin fin. La corrección del docente pidió verificar que una ejecución
sobreviva a un reinicio.

## Decisión

1. **Cola confiable:** el worker **mueve** el mensaje de forma atómica (`BLMOVE cola:tareas →
   cola:procesando`) y lo borra (`LREM`) recién cuando el job queda en `DONE`, `FAILED` o
   `WAITING_APPROVAL`. Una cancelación (`CancelledError` al apagar) no confirma el mensaje.
2. **Recuperación al arrancar:** antes de lanzar los workers, `recuperar_huerfanos()` devuelve a la
   cola todo lo que quedó en `cola:procesando`, en su orden original y antes que los nuevos.
3. **Retomar, no repetir:** `elegir_entrada()` lee el checkpoint del hilo antes de invocar. Si quedan
   nodos pendientes, invoca con `None` ("continúa desde el último checkpoint"): no repite los nodos
   terminados ni duplica la pregunta en `messages`.

## Alternativas consideradas

| Alternativa | Por qué no |
|---|---|
| Dejar `BRPOP` y marcar `FAILED` lo que quede en `RUNNING` | Pierde trabajo que se podía retomar desde el checkpoint |
| Redis Streams con consumer groups (`XREADGROUP` + `XAUTOCLAIM`) | Resuelve también el caso multi-réplica, pero es más complejo de lo necesario con una sola instancia |
| Celery con `acks_late` | Ver ADR 0002 |

## Consecuencias

- ➕ Verificado con la pila real: `SIGKILL` a mitad del investigador → al arrancar, "Se recuperaron 1
  jobs interrumpidos" → el proceso nuevo continúa desde el paso 1 del grafo. Además, el escenario 5
  de las pruebas de punta a punta lo repite en cada corrida.
- ➖ **Una sola instancia:** al arrancar, todo lo que hay en `cola:procesando` se considera huérfano.
  Con varias réplicas habría que registrar qué instancia tomó cada mensaje (con un heartbeat) o
  pasar a consumer groups.
- ➖ Se repite el nodo que estaba corriendo al morir (el checkpoint es por paso). Por eso los efectos
  laterales tienen que ser idempotentes (ADR 0007).
