# Decisiones de arquitectura (ADR)

Cada archivo registra una decisión: el contexto, lo que se decidió, las alternativas descartadas y
sus consecuencias, con evidencia medida cuando la hay. Formato inspirado en
[Michael Nygard](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions).

| # | Decisión | En una línea |
|---|---|---|
| [0001](0001-supervisor-con-especialistas.md) | Supervisor con especialistas en LangGraph | Un supervisor rutea con una rúbrica; investigador, analista y gestor con contexto acotado |
| [0002](0002-api-asincrona-con-cola-en-redis.md) | API asíncrona con cola en Redis | 202 inmediato; workers asyncio en el mismo proceso; async de punta a punta |
| [0003](0003-redis-como-estado-cola-y-checkpointer.md) | Redis para todo el estado | Jobs, cola, eventos, idempotencia y checkpoints de LangGraph en un solo Redis |
| [0004](0004-cola-confiable-y-recuperacion.md) | Cola confiable y recuperación | `BLMOVE` + huérfanos al arrancar + retomar desde el checkpoint |
| [0005](0005-rag-hibrido-con-reranking.md) | RAG híbrido con re-ranking | BM25 + Pinecone + cross-encoder: Acierto@1 de 72 % a 94 % |
| [0006](0006-contratos-pydantic-en-las-fronteras.md) | Contratos Pydantic en las fronteras | Config, API, salida del LLM, tools (entrada y artefactos) y estado |
| [0007](0007-resiliencia-por-capas-e-idempotencia.md) | Resiliencia por capas e idempotencia | Un criterio de error transitorio; registro único por (job, tipo) |
| [0008](0008-aprobacion-humana-con-interrupt.md) | Aprobación humana con `interrupt()` | La criticidad se decide en código; la pausa no ocupa workers |
| [0009](0009-observabilidad-con-phoenix.md) | Observabilidad con Phoenix | Trazas, costo y desglose por nodo; evidencia generada desde los datos |
| [0010](0010-streaming-sse-via-redis-streams.md) | Streaming con SSE y Redis Streams | El worker publica; la API transmite; reconexión con `Last-Event-ID` |
