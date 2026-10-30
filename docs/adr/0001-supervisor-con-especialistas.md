# ADR 0001 · Supervisor con especialistas en LangGraph

**Estado:** aceptada · **Fecha:** 2026-10

## Contexto

Las preguntas de los vecinos mezclan tres tipos de trabajo: buscar la norma en el reglamento,
calcular (multas, horas, montos) y, a veces, ejecutar una acción con efectos reales (registrar una
mudanza). Un solo agente con todas las herramientas tendía a calcular de memoria o a citar sin
fuente; una cadena fija (buscar → calcular → responder) no sirve cuando la pregunta no necesita
cálculo o cuando hay que volver a buscar con otros términos.

## Decisión

Un grafo de LangGraph con un **supervisor** que decide en cada vuelta quién interviene, y tres
**especialistas** con contexto acotado: el investigador (solo busca, con el RAG), el analista
(solo calcula, con una calculadora AST) y el gestor (solo registra, detrás de aprobación humana).
Una **síntesis** redacta la respuesta final con los aportes.

- El supervisor decide con salida estructurada (`DecisionSupervisor`) a partir de una rúbrica
  explícita: datos con fuente, cálculo verificable, coherencia y completitud. Si un aporte no la
  cumple, reenvía al mismo especialista con una instrucción de corrección.
- Cada especialista recibe **solo la instrucción** del supervisor, no la conversación completa.
- Guardas deterministas en código: `MAX_PASOS` corta los ciclos; la síntesis rechaza cifras que no
  aparezcan en los aportes; el gestor solo cuenta como registro el artefacto de la herramienta.

## Alternativas consideradas

| Alternativa | Por qué no |
|---|---|
| Un agente ReAct con todas las tools | Mezcla responsabilidades: calculaba "de cabeza" y no siempre citaba |
| Cadena LCEL fija | No tiene ciclos: no puede volver a buscar ni saltarse el cálculo |
| Enjambre (agentes que se pasan el control) | Más difícil de acotar y de auditar; el flujo de este dominio es jerárquico |

## Consecuencias

- ➕ Cada especialista es simple y testeable por separado; el flujo es auditable en las trazas.
- ➕ Autocorrección: la rúbrica del supervisor y los errores de validación de las tools devuelven el
  trabajo al especialista (ver ADR 0006 y 0007).
- ➖ **Costo:** el supervisor corre ~2,6 veces por pregunta y su contexto crece con cada aporte. En
  las pruebas de punta a punta es el **42 % del costo** total
  ([evidencia](../evidencia/pruebas-e2e.md#costo-y-latencia-por-nodo-todas-las-pruebas)).
  Mejora posible: un modelo más chico solo para el ruteo, o pasarle un resumen de los aportes.
