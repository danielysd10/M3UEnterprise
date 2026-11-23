# Configuración personal y recuperación de trabajos

La configuración personal guarda las preferencias, los bloques de URL y el catálogo de favoritos. El estado de un trabajo guarda los resultados que se pueden reutilizar al reanudarlo. Elija la carpeta de salida del M3U y su informe al iniciar el trabajo.

| Sistema | Configuración personal | Checkpoints |
|---|---|---|
| Windows | `%LOCALAPPDATA%\M3UEnterprise\config` | `%LOCALAPPDATA%\M3UEnterprise\state\checkpoints` |
| macOS | `~/Library/Application Support/M3UEnterprise/config` | `~/Library/Application Support/M3UEnterprise/state/checkpoints` |
| Linux | `$XDG_CONFIG_HOME/m3u-enterprise` o `~/.config/m3u-enterprise` | `$XDG_STATE_HOME/m3u-enterprise/checkpoints` o `~/.local/state/m3u-enterprise/checkpoints` |

## Elegir un perfil

Antes de abrir la aplicación puede fijar dos rutas absolutas. `M3U_CONFIG_DIR` designa directamente la carpeta de configuración y `M3U_STATE_DIR` contiene la subcarpeta `checkpoints`. Por ejemplo, para usar un perfil de prueba en una sesión de PowerShell:

```powershell
$perfil = Join-Path $env:TEMP ('m3u-perfil-' + [guid]::NewGuid())
$env:M3U_CONFIG_DIR = Join-Path $perfil 'config'
$env:M3U_STATE_DIR = Join-Path $perfil 'estado'
python -B run.py
```

Para continuar ese mismo trabajo, vuelva a usar esas dos rutas. Generar otro identificador crea un perfil diferente.

## Guardar y recuperar preferencias

Use los botones de guardado de los editores de bloques y favoritos. La GUI guarda las preferencias del trabajo al iniciar la unificación. Los archivos personales son `unificador_m3u_config.json`, `bloques_urls.json` y `favoritos_maestro.txt`. En una instalación anterior puede existir el alias `favoritos.txt`. Un catálogo personal vacío también tiene prioridad.

Para importar una configuración anterior, indique la carpeta existente que contiene sus archivos. En este ejemplo se llama `perfil-anterior` y está en el directorio actual.

```powershell
python -B -m FAVORITOS_CATALOGO_ENTERPRISE --import-legacy ".\perfil-anterior"
```

La importación conserva los originales y copia sólo los archivos que faltan en el destino. Si muestra «conflicto», compare las versiones antes de elegir qué guardar. El código de salida es 0 al completar sin conflictos y 1 si hay conflictos o errores. Puede repetir una importación interrumpida.

## Reanudar la validación

Vuelva a elegir las mismas entradas, opciones y carpeta de estado. Un cambio de ruta, tamaño, fecha o configuración puede corresponder a otro trabajo. El orquestador reutiliza los resultados que siguen vigentes. Use «Detener» y espere a que termine antes de cerrar. El informe y el M3U de salida se consultan en la carpeta elegida por el operador.

Con la ubicación predeterminada, el orquestador puede recuperar el SQLite cerrado del mismo trabajo junto a los módulos de una instalación anterior. Verifica identidad, esquema e integridad antes de copiarlo. Un destino existente tiene prioridad. Una ruta `M3U_STATE_DIR` explícita desactiva esa búsqueda junto a los módulos.

Si la aplicación informa que el checkpoint está activo, cierre limpiamente el proceso que lo usa y reintente. Conserve juntos el SQLite y sus archivos auxiliares. Si aparece un error de integridad o de identidad, conserve el original para diagnóstico. Los checkpoints pueden contener información privada del trabajo. [Guía de operación](OPERATIONS_RUNBOOK.md).
