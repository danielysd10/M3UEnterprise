# M3U Enterprise — Contrato de Prevalidación 1.0

## Flujo

`ORIGINAL → M3U_PRO_VALIDATOR 10.1.0 → M3U_PRO_REPARADOR 10.1.0 → CLEAN M3U → PREVALIDATION_MANIFEST.json → UNIFICADOR ENTERPRISE → FFprobe → FINAL ENTERPRISE M3U`

## Semántica

`PREVALIDATED` certifica únicamente parsing/estructura, sintaxis y clasificación URL, reparaciones deterministas y publicación íntegra del archivo limpio. Nunca certifica disponibilidad ni actividad del stream.

## Evidencia

El Unificador solo acepta prevalidación cuando se verifican: manifest 1.0, contrato correcto, Validator 10.1.0, Repairer 10.1.0, registro único del archivo, estado publicable y SHA-256 coincidente.

## Fail-safe

Manifest ausente, corrupto, incompatible, archivo no certificado, SHA mismatch o archivo ilegible producen `PREVALIDATED=false` y fallback al comportamiento normal.

## FFprobe

FFprobe permanece como juez profundo. Sigue determinando conexión/apertura del stream, pistas multimedia, vídeo, codec, resolución, bitrate, errores, timeout y estado activo/muerto según las reglas existentes.

## Orígenes

Solo las entradas procedentes del archivo cuyo SHA fue certificado reciben el bypass de URL preflight. URLs manuales y Favorites manuales continúan usando la política URL normal.

## Checkpoints

La identidad del job/fingerprint incorpora el contexto de prevalidación para evitar reutilización incorrecta de resultados entre contratos diferentes.

## Observabilidad contractual

Cuando se solicita la prevalidación certificada, el informe del Unificador debe dejar evidencia explícita de: `Solicitada`, archivos certificados por manifest + SHA-256, entradas certificadas, coincidencias SHA-256, mismatches, entradas para las que se omitió el URL prevalidation y archivos que hicieron fallback con su motivo.

La ausencia de esta evidencia se considera una deficiencia de observabilidad, aunque el procesamiento funcional continúe correctamente.
