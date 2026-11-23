# Operación y recuperación

## Arranque normal

Desde la carpeta extraída que contiene `run.py`, ejecute `python -B run.py`. Para comprobar la instalación o inspeccionar una lista local desde la terminal, consulte las guías que acompañan al ZIP. [Instalación](INSTALLATION_V9_0_2.md) · [Uso operativo](USAGE_V9_0_2.md).

## Autocontroles

Ejecute `python -B run_self_tests.py` en una extracción de prueba con un perfil personal temporal. La guía de instalación describe cómo fijar las carpetas de configuración y estado y cómo ejecutar la suite completa.

## Medición local sin red

```text
python -B tools/stress_harness.py --sizes 1000
```

El comando genera datos sintéticos en una carpeta temporal y muestra registros leídos, duplicados, entradas en cuarentena y tiempos de análisis. Use `--sizes 1000 10000 50000` para comparar tamaños. También puede medir 100000 y 180000 entradas.

## Recuperación tras una interrupción

1. Conserve los archivos de entrada y los parámetros originales, incluida la carpeta de estado del trabajo.
2. Mantenga `validacion_checkpoint_<jobid>.sqlite3` en su carpeta de checkpoints. Si necesita trasladarla a otro equipo, cierre los procesos que la utilizan y siga el procedimiento de recuperación. [Configuración y recuperación](CONFIGURATION_AND_RECOVERY.md).
3. Ejecute de nuevo el trabajo con los mismos parámetros. Lea en el informe «Resultados reutilizados de checkpoint» para conocer cuántas validaciones se aprovecharon y compruebe el M3U de salida.

## Recuperación tras fallo de reporte

El output M3U no se invalida. Buscar:

```text
<salida>_informe.txt
<salida>_informe_fallback.txt
```

## Diagnóstico de una URL

Para pruebas unitarias se puede llamar a:

```python
from url_safety import inspect_url, redact_url

result = inspect_url("http://example.com:bad/live")
print(result.status, result.reason)
print(redact_url("http://user:secret@example.com/live?token=abc"))
```

## Saneamiento de credenciales

No copiar URLs completas desde datasets privados a tickets/logs. Los checkpoints nuevos almacenan la URL redaccionada.

## Actualización

Antes de reemplazar el paquete, conservar la carpeta de estado si se necesita reanudar jobs existentes. No copie bases SQLite corruptas al nuevo entorno sin una copia de respaldo.
