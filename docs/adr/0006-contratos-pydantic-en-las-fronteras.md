# ADR 0006 · Contratos Pydantic en cada frontera

**Estado:** aceptada · **Fecha:** 2026-10

## Contexto

En un sistema con LLM, los datos cruzan fronteras poco confiables: lo que envía un cliente HTTP, lo
que el modelo decide pasarle a una herramienta, lo que el modelo responde, y lo que vuelve de un
checkpoint. Un error que no se detecta en la frontera aparece después, lejos de su causa.

## Decisión

| Frontera | Contrato |
|---|---|
| Configuración | `Settings` (`pydantic-settings`): la API no arranca si falta una clave o un valor es inválido |
| API HTTP | Modelos de entrada y salida, incluidos los errores (`ErrorRespuesta`) y `/health`; `job_id` con patrón (formato inválido → 422 sin tocar Redis) |
| Salida del LLM | `with_structured_output(include_raw=True)` + `validar_respuesta_completa`: rechaza respuestas truncadas **aunque hayan calzado con el esquema**, y las que no se pudieron parsear |
| Entrada de tools | `args_schema` (por ejemplo, la calculadora solo acepta números y operadores) |
| Salida de tools | `response_format="content_and_artifact"`: el LLM recibe texto; el código, un objeto validado |
| Estado del grafo | `Contribucion` es `TypedDict` y se crea solo con `nueva_contribucion()`, que la valida |

## Decisiones finas

- **Truncación antes que parseo:** un JSON cortado puede calzar con el esquema (un `str` a medio
  escribir es válido). Revisar primero `finish_reason` evita aceptar una instrucción incompleta.
- **`TypedDict` validado, no `BaseModel`, en el estado:** el estado vive en los checkpoints. Un dict
  se serializa sin acoplar los checkpoints a una clase de la app; el contrato se cumple al crear.
- **Re-validar lo que vuelve de un checkpoint:** con la pila real se detectó que, al retomar el
  subgrafo del gestor, su artefacto `SolicitudRegistrada` vuelve como `dict` serializado de LangChain
  (`{"lc": 2, "type": "constructor", ...}`): el serializador no reconstruye clases de la app. Se valida
  de nuevo con Pydantic en vez de confiar en `isinstance` (había un falso "no se registró").

## Consecuencias

- ➕ **Autocorrección:** si el modelo llama a la calculadora con `"5 UTM * 2"`, el `ToolNode` le
  devuelve el error de validación y el modelo reintenta con `"5 * 2"` (hay test).
- ➕ El número de solicitud que ve el vecino sale del registro real, no del texto del modelo.
- ➕ OpenAPI documenta cada respuesta posible (200, 404, 409, 422, 503).
- ➖ Más modelos que mantener; los tests de contrato fallan cuando cambia la forma de un dato (es la idea).
