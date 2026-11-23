# Arquitectura endurecida

## 1. Capas

### Ingesta
`parse_m3u()` y `parse_manual_block()` convierten texto externo en registros estructurados.

### Política URL
`url_safety.py` es la única capa que interpreta sintaxis básica, host, puerto, credenciales y protocolos soportados.

No se confunde:

```text
válida sintácticamente != accesible != reproducible
```

### Reparación
Solo se realizan reparaciones deterministas de alta confianza. Para el incidente se reconoce el wrapper legacy RTMP y se conserva el original.

### Cuarentena
Antes de cualquier red/FFprobe, `_quarantine_invalid_urls()` separa entradas utilizables de entradas inválidas/no soportadas.

La puerta se aplica incluso cuando la validación de red está desactivada, evitando publicar por accidente una URL semánticamente inválida.

### Validación
`enterprise_validate_all()` limita futures pendientes, captura excepciones por tarea, conserva resultados en SQLite y reanuda desde fingerprints.

### FFprobe
`enterprise_ffprobe()` y `validate_url_ffprobe()` aplican primero la política URL. El subprocess tiene timeout; stderr se trunca y redacciona antes de persistirlo.

### Reporting
`save_report()` usa representación URL segura y writes atómicos. El worker publica primero el M3U y después intenta el reporte. Un fallo de reporte genera fallback y no borra la salida publicada.

## 2. Estado persistente

```text
INPUTS
  -> fingerprint
  -> SQLite results[fingerprint]
  -> VALIDATION_COMPLETE
  -> publish output atomically
  -> report
  -> PUBLISHED
```

SQLite reemplaza la estrategia de reescribir un JSON creciente cada N resultados. El JSON legacy sigue soportado como fuente de migración.

## 3. Aislamiento de fallos

La intención es que una excepción tenga el menor radio de impacto posible:

```text
registro -> resultado de error
worker -> registra error, continúa
reporte -> fallback, conserva salida
job -> checkpoint durable, permite resume
```

## 4. Redacción

```text
raw value
  -> inspect_url()
  -> urlsplit seguro
  -> credential removal
  -> query/path redaction
  -> report-safe string
```

También se aplica redacción a textos de excepción antes de logs/checkpoints.

## 5. Concurrencia

Se mantiene `ThreadPoolExecutor` porque el workload es principalmente I/O. El pipeline limita a 8 workers globales y 2 operaciones simultáneas por host, con jitter/gap configurable existente para estabilidad.

No se introduce `ProcessPoolExecutor` porque aumentaría complejidad de serialización y no aporta ventaja para FFprobe/red.

## 6. Integridad de salida

La salida M3U se genera en memoria, se audita estructuralmente y se publica mediante `atomic_write_utf8()`. La publicación antecede al reporte.

Una futura evolución para >1M registros debería pasar a un modelo por chunks/streaming para evitar materialización masiva de `all_entries`, `unique_candidates` y `final_entries`.
