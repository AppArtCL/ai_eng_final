# ADR 0005 · RAG híbrido con re-ranking hospedado en Pinecone

**Estado:** aceptada · **Fecha:** 2026-10

## Contexto

El reglamento mezcla términos exactos (UTM, "parrilla a carbón", números de departamento) con
preguntas que los vecinos formulan con otras palabras ("¿puedo atender clientes de mi peluquería?"
→ *destino exclusivo habitacional*). El RAG del M4 combinaba BM25 y búsqueda vectorial con RRF, pero
RRF solo fusiona **posiciones**: el fragmento correcto aparecía entre los 5, aunque no siempre primero.

## Decisión

Recuperación en dos etapas:

1. **Híbrida:** BM25 (con preprocesamiento en español) y Pinecone traen 20 candidatos cada uno
   (`RAG_CANDIDATOS`), fusionados con `EnsembleRetriever` (RRF).
2. **Re-ranking:** un cross-encoder (`bge-reranker-v2-m3`, hospedado en Pinecone) puntúa cada par
   (pregunta, fragmento) y deja los 5 mejores (`RAG_TOP_K`). El puntaje llega al agente como
   "Relevancia".

Si el re-ranking falla (límite del plan, red), `with_fallbacks` entrega los 5 primeros del híbrido.

## Alternativas consideradas

| Alternativa | Por qué no |
|---|---|
| Cross-encoder local (sentence-transformers) | Síncrono y CPU-bound (habría que sacarlo del event loop) y suma más de 1 GB a la imagen |
| Cohere Rerank | API key y proveedor adicionales |
| Re-ranking con el mismo LLM | Una llamada extra al LLM por búsqueda: más lento y más caro |

Pinecone ya era dependencia, usa la misma API key y su cliente es asíncrono.

## Evidencia

Golden set de 18 preguntas, evaluado por **sección** y por **orden**
([reporte](../evidencia/evaluacion-rag.md), `uv run python -m app.rag.evaluate`):

| Sistema | Acierto@1 | MRR | Recall@5 |
|---|---|---|---|
| Híbrido (RRF) | 72 % | 0,85 | 100 % |
| **Híbrido + re-ranking** | **94 %** | **0,97** | 100 % |

## Consecuencias

- ➕ El fragmento correcto queda primero en 17 de 18 preguntas (antes, 13). Caso emblemático del M4,
  "¿Puedo hacer asados?": del 2.º al 1.er lugar.
- ➖ Una regresión documentada: "¿Cuánto me cobran si rompo una regla por segunda vez?" baja del 1.º
  al 2.º lugar (sigue dentro del top 5).
- ➖ +~0,5 s por búsqueda y dependencia de la cuota de inference de Pinecone (mitigada por el fallback).
- ➖ El SDK de Pinecone no cierra su cliente async del re-ranker: la API lo cierra al apagar.
