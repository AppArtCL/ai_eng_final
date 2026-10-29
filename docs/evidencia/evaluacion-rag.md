# Evaluación de la recuperación: híbrido vs. híbrido + re-ranking

Golden set de 18 preguntas (`app/rag/golden_set.json`), k = 5 fragmentos, re-ranker `bge-reranker-v2-m3` (Pinecone). Generado con `uv run python -m app.rag.evaluate`.

| Sistema | Acierto@1 | MRR | Recall@5 |
|---|---|---|---|
| Híbrido (BM25 + Pinecone, RRF) | 72% | 0.85 | 100% |
| Híbrido + re-ranking | 94% | 0.97 | 100% |

## Detalle por pregunta (posición de la sección correcta)

| # | Tipo | Pregunta | Híbrido (BM25 + Pinecone, RRF) | Híbrido + re-ranking |
|---|---|---|---|---|
| 1 | lexica | ¿A cuántos centímetros del suelo debo guardar las cosas en la bodega? | 1.º | 1.º |
| 2 | semantica | Si me atraso en pagar los gastos comunes, ¿me pueden cortar la luz? | 2.º | 1.º |
| 3 | semantica | ¿Puedo llevar amigos que no viven en el edificio a la piscina? | 1.º | 1.º |
| 4 | lexica | ¿Se pueden hacer mudanzas los domingos? | 1.º | 1.º |
| 5 | lexica | ¿Está permitido guardar materiales inflamables en la terraza? | 1.º | 1.º |
| 6 | ambigua | ¿Puedo hacer asados? | 2.º | 1.º |
| 7 | lexica | ¿Puedo tener mascota en mi departamento? | 1.º | 1.º |
| 8 | semantica | ¿Cuánto me cobran si rompo una regla del edificio por segunda vez? | 1.º | 2.º |
| 9 | semantica | ¿Puedo poner música fuerte a medianoche? | 2.º | 1.º |
| 10 | lexica | ¿Para cuántas personas como máximo se puede reservar el salón multiuso? | 1.º | 1.º |
| 11 | semantica | ¿Puedo dejar mi bicicleta en el pasillo frente a mi puerta? | 1.º | 1.º |
| 12 | semantica | ¿Puedo cambiar el piso de mi departamento por porcelanato? | 1.º | 1.º |
| 13 | semantica | ¿A qué hora pueden trabajar los maestros que remodelan el departamento de al lado? | 1.º | 1.º |
| 14 | semantica | Mi hermano viene de visita en auto, ¿cuánto tiempo puede dejarlo estacionado? | 1.º | 1.º |
| 15 | semantica | ¿Puedo colgar la ropa a secar en el balcón? | 1.º | 1.º |
| 16 | semantica | ¿El edificio tiene un lugar para atender una emergencia médica menor? | 2.º | 1.º |
| 17 | lexica | ¿Quién mantiene los jardines de los departamentos 101 y 104? | 1.º | 1.º |
| 18 | semantica | ¿Puedo atender clientes de mi peluquería en mi departamento? | 4.º | 1.º |
