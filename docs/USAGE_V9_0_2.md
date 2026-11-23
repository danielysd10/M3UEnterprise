# Validar una lista y leer sus resultados

## Inspección local desde la terminal

Abra una terminal en la raíz extraída y utilice una lista M3U de prueba. Sustituya las rutas entre comillas por las suyas y elija una ruta nueva para el informe. Este comando inspecciona el archivo sin solicitar conexiones de red.

```powershell
python -B M3U_PRO_VALIDATOR.py "ejemplo.m3u" --report "informe.json"
```

La consola indica archivos procesados, entradas y ruta del informe JSON. El código de salida 0 indica que todos los archivos procesados pasaron la validación solicitada. El informe de esta inspección describe la estructura y las URLs. Para conocer la disponibilidad o la reproducción de un stream, hay que sondearlo desde un entorno con acceso autorizado.

## Unificación con la interfaz gráfica

1. Desde la raíz extraída, ejecute `python -B run.py`. Pulse **Agregar listas**, seleccione las listas M3U/M3U8 y, si corresponde, añada entradas manuales.
2. Revise Favoritos y configure hasta cuatro fuentes por canal lógico. Seleccione la carpeta y el archivo de salida.
3. La GUI Enterprise usa ocho trabajadores. El tiempo de espera inicial es de ocho segundos en un perfil nuevo y puede modificarse desde la interfaz. Elija la validación HTTP/HLS o el sondeo profundo con FFprobe según el acceso autorizado para el trabajo. [Perfiles de red](OPERATIONS_PROFILES.md).
4. Inicie la unificación y consulte el informe junto al M3U de salida. Un estado `TIMEOUT`, `UNKNOWN` o `NETWORK_ERROR` requiere revisar el diagnóstico y las condiciones de red antes de descartar el canal.
5. Si pulsa **Detener**, vuelva a abrir el trabajo con los mismos archivos, parámetros y perfil de estado para reutilizar los resultados vigentes. El tiempo de caché de la validación es de 15 minutos para los éxitos y 60 segundos para los fallos. [Operación y recuperación](OPERATIONS_RUNBOOK.md).
