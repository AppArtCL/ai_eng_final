# ADR 0009 · Observabilidad con Arize Phoenix y OpenInference

**Estado:** aceptada · **Fecha:** 2026-10

## Contexto

Un sistema multi-agente que "funciona" pero es una caja negra no se puede operar: hay que ver qué
decidió el supervisor, qué recuperó el RAG, cuánto costó cada ejecución y dónde se va la latencia.

## Decisión

- **Arize Phoenix** autohospedado en `docker compose` (sin cuentas ni envío de datos a terceros),
  instrumentado con **OpenInference** (`LangChainInstrumentor`): un span por nodo, cadena, llamada al
  LLM (con tokens, que Phoenix convierte en costo), herramienta y retriever (BM25, Pinecone, re-ranker).
- El worker abre un span raíz `job` con `session.id = job_id`: la pausa y la reanudación de un HITL
  quedan en la misma sesión. Un job retomado tras un reinicio se marca `job.retomado = true`.
- Exportación por lotes (`BatchSpanProcessor`) para no frenar a los workers. Las peticiones HTTP no se
  trazan: el polling inflaría el conteo y falsearía la latencia de las ejecuciones.
- La evidencia se genera **desde los datos**, no a mano: `scripts/e2e.py` consulta la API GraphQL de
  Phoenix (costo, latencia y desglose por nodo sumando los LLM que cuelgan de cada nodo) y captura el
  árbol de spans con Chrome headless.

## Alternativas consideradas

| Alternativa | Por qué no |
|---|---|
| LangSmith | SaaS con cuenta y API key adicionales; los datos salen de la máquina |
| Solo logs | No muestran el árbol de decisiones ni el costo por ejecución |

## Consecuencias

- ➕ El desglose por nodo responde dónde optimizar: el supervisor es el **42 % del costo** y el
  investigador el nodo más lento (**4,4 s** promedio) ([evidencia](../evidencia/pruebas-e2e.md)).
- ➖ Con `SIGKILL` se pierden los spans aún en el buffer (hasta ~5 s): la traza de la vida que murió
  no llega a Phoenix. Por eso la recuperación marca `job.retomado`, y las pruebas esperan a que salga
  el lote antes de matar la API.
- ➖ Phoenix calcula el costo solo para modelos que conoce: un modelo nuevo puede figurar sin costo.
