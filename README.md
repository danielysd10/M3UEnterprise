# M3U Enterprise

M3U Enterprise reúne listas M3U/M3U8, detecta entradas dañadas y permite reanudar la validación de un trabajo. Para empezar con el ZIP de fuentes, abra una terminal en la carpeta extraída que contiene `run.py`.

| Tarea | Guía |
|---|---|
| Comprobar el ZIP y su SBOM | [Verificación de la entrega](docs/RELEASE_PACKAGING.md) |
| Preparar Python y abrir la interfaz | [Instalación](docs/INSTALLATION_V9_0_2.md) |
| Validar una lista y leer el informe | [Uso operativo](docs/USAGE_V9_0_2.md) |
| Reanudar una operación o diagnosticar un fallo | [Manual de operación](docs/OPERATIONS_RUNBOOK.md) |

## Objetivo

Esta versión reingenierizada conserva la funcionalidad Enterprise V9 y corrige el patrón de fallo que permitió que una entrada M3U malformada derribara el proceso después de horas de trabajo.

Principio operativo:

```text
entrada externa
  -> parsing
  -> política central de URL
  -> reparación determinista / cuarentena
  -> deduplicación
  -> validación HTTP/HLS o FFprobe
  -> persistencia de resultados
  -> ensamblado y auditoría
  -> publicación atómica
  -> reporte aislado con fallback
```

Una entrada corrupta debe convertirse en un registro diagnosticable, no en una excepción global.

## Alcance probado

La interfaz instalada se probó en Windows al 150 % real, con Python 3.13 y 3.14. En ese equipo se verificaron los lanzadores, el aprovisionamiento de FFprobe y el guardado de la configuración personal con la instalación protegida contra escritura. El rendimiento observado con datos sintéticos depende del volumen, del número de fuentes por host y del estado del checkpoint. La operación prolongada con proveedores reales requiere mediciones en el entorno autorizado.

## Requisitos

- Windows 10/11 recomendado para el entorno operativo histórico del proyecto.
- Python 3.13.x o 3.14.x, conforme a `pyproject.toml` (`>=3.13,<3.15`).
- Tkinter, normalmente incluido en las instalaciones oficiales de Python para Windows.
- FFprobe para validación profunda. El programa busca `ffprobe.exe` en PATH y en ubicaciones configuradas; también conserva el mecanismo existente de descarga portable.
- Sin dependencias Python obligatorias de terceros.

El paquete conserva lanzadores para instalaciones anteriores. [Compatibilidad](COMPATIBILITY.md) · [Lanzadores históricos](M3U_PRO/README.md).

## Instalación

Verifique el par ZIP/SBOM con una copia confiable del verificador y extraiga el ZIP en una carpeta nueva. Desde la carpeta que contiene `run.py`, compruebe la versión de Python e inicie la interfaz.

```text
python --version
python -B run.py
```

Para el arranque desde PowerShell, la selección de FFprobe y las herramientas opcionales de pruebas, consulte la [guía de instalación](docs/INSTALLATION_V9_0_2.md).

## Configuración personal

En Windows, preferencias, bloques y favoritos se guardan en `%LOCALAPPDATA%\M3UEnterprise\config`. `M3U_CONFIG_DIR` permite elegir otra carpeta absoluta; los checkpoints siguen usando `M3U_STATE_DIR`. La instalación puede permanecer sin permisos de escritura para los flujos verificados. La lectura de catálogos heredados no los modifica.

Para copiar configuración anterior, sustituya `CARPETA_ANTERIOR` por la carpeta que contiene sus archivos y ejecute `python -m FAVORITOS_CATALOGO_ENTERPRISE --import-legacy "CARPETA_ANTERIOR"`. La importación conserva el origen y cualquier destino existente, y notifica conflictos. [Guía de configuración y recuperación](docs/CONFIGURATION_AND_RECOVERY.md).

## Interfaz adaptable

En equipos con pantalla baja, escalado DPI alto o una ventana reducida, el formulario central se desplaza mediante la barra vertical, rueda del mouse, `Page Up`/`Page Down` y `Home`/`End`. La barra de estado, cancelación y los botones de ejecución quedan fijos al pie, por lo que siempre se puede iniciar, reparar o detener una operación.

## Perfiles de red y publicación HTTPS

Todo flujo que abra red debe usar un perfil `development`, `campaign` o `production`. `production` falla cerrado si no existen `M3U_OUTBOUND_PROXY` y `M3U_EGRESS_GUARD=1`; no habilite `M3U_ALLOW_PRIVATE_NETWORKS=1` en ese perfil. El Validador y el enriquecedor aceptan `--profile`; el transporte HTTP también aplica el perfil activo como barrera final.

Para preparar una lista junto con su guía XMLTV, sustituya el dominio del ejemplo por la dirección HTTPS de su infraestructura aprobada. [Perfiles de red](docs/OPERATIONS_PROFILES.md) · [Publicación de listas](docs/PLAYLIST_PUBLICATION.md).

```text
python tools/publish_playlist.py --input lista.m3u --epg-input lista_guia.xml --publish-root ./publicacion --public-base-url https://iptv.example.org
```

Esto publica `iptv/unificada.m3u`, `iptv/guia.xml` y `iptv/unificada.manifest.json` con hashes independientes, y cambia `url-tvg` a la URL HTTPS pública de la guía. No use el servidor local de desarrollo como sustituto de infraestructura HTTPS de producción.

## Integridad y supply chain

El ZIP de fuentes incluye su manifiesto `SHA256SUMS_OPTIMIZED.txt` y el inventario `SBOM.cdx.json`. Para una entrega firmada, el workflow de GitHub comprueba dos declaraciones sobre el mismo ZIP. Una identifica la procedencia de la compilación y la otra contiene el SBOM distribuido. Consulte los pasos para verificar una entrega recibida. [Guía de empaquetado](docs/RELEASE_PACKAGING.md).

Al recibir una entrega, use una copia confiable del verificador y ejecute `python -B tools/verify_release.py --zip RUTA_AL_ZIP --sbom RUTA_AL_SBOM`. El código de salida 0 confirma que el manifiesto y el SBOM externo coinciden con el contenido del ZIP. El comando muestra el SHA-256 de cada archivo. La guía explica qué archivos se distribuyen y cómo reintentar una publicación interrumpida en una carpeta nueva. [Guía de empaquetado y verificación](docs/RELEASE_PACKAGING.md).

## FFprobe / FFmpeg

Para validación profunda, instalar FFmpeg/FFprobe y asegurar que `ffprobe.exe` esté en PATH, o usar la ruta portable/configurada por la aplicación.

La aplicación ejecuta FFprobe con un tiempo límite. Un timeout, un código de salida distinto de cero, un JSON inválido o un error de ejecución se registran en el resultado de la entrada afectada.

## Entradas

El pipeline acepta listas M3U/M3U8 y URLs manuales. El parser conserva `#EXTINF`, directivas relevantes, grupos y metadatos existentes antes de aplicar las reglas de normalización ya presentes en V9.

Las URLs se inspeccionan de forma centralizada mediante `url_safety.py`.

Estados principales:

- `VALID`
- `REPAIRED`
- `INVALID_SYNTAX`
- `INVALID_PORT`
- `INVALID_HOST`
- `UNSUPPORTED_SCHEME`
- `EMPTY`
- `NON_STRING`

Para el incidente original se reconoce de forma determinista el wrapper:

```text
rtmp://$OPT:rtmp-raw=rtmp://<host>/<path>
```

Se conserva el valor original en metadatos (`url_original`) y se utiliza el objetivo RTMP interno como URL de red.

## Salidas

- M3U consolidado.
- Informe de unificación.
- Informe fallback si el reporte principal falla.
- Estado de validación persistido en SQLite por job.

## Logos oficiales y guía EPG/XMLTV

La GUI incorpora el bloque **Metadatos IPTV: logos oficiales y guía XMLTV**. Su alcance predeterminado es `favorites`: sólo las categorías `00 - Favoritos …` se enriquecen, de modo que no se añade latencia proporcional a toda la colección. El modo `all` es explícito.

Seleccione un registro JSON local basado en [`channel_metadata_registry.example.json`](channel_metadata_registry.example.json). El esquema 2 rechaza aliases, `tvg-id` y `epg-id` ambiguos. Cada logo añadido exige `official_logo_url` HTTPS y `official_logo_source` HTTPS: la herramienta nunca sustituye un logo existente ni adivina que un recurso comunitario sea oficial. Los canales sin coincidencia quedan intactos y se contabilizan en el informe de métricas.

Si el registro define `epg_url` HTTPS, la salida incorpora `#EXTM3U url-tvg="…"`, que los reproductores IPTV compatibles utilizan para cargar programación XMLTV. La opción de descarga guarda XML descomprimido en `<salida>_guia.xml`, con TTL de 12 horas y límites de descarga/expansión. Antes de cada redirección se aplica la política SSRF; el XMLTV no modifica URLs de stream ni acredita que un canal reproduzca. La ejecución registra cobertura agregada de `tvg-id` y programación por categoría.

También puede ejecutar el flujo sin GUI:

```text
python tools/enrich_m3u_metadata.py --input lista.m3u --output lista_enriquecida.m3u --registry channel_metadata_registry.json --scope favorites --xmltv-output artifacts/campaign/guia.xml
```

Consulte [`docs/METADATA_ENRICHMENT.md`](docs/METADATA_ENRICHMENT.md) para el contrato del registro, límites y operación reproducible. Si aún no cuenta con proveedores propios, [`docs/EXTERNAL_METADATA_ONBOARDING.md`](docs/EXTERNAL_METADATA_ONBOARDING.md) describe un descubrimiento de candidatos sin streams y la revisión necesaria antes de declarar un logo o EPG como autorizado.

La publicación del M3U es atómica; la ausencia o fallo del reporte no invalida una salida ya publicada.

## Checkpoint / Resume

En Windows, el estado nuevo se guarda fuera de la instalación en:

```text
%LOCALAPPDATA%\M3UEnterprise\state\checkpoints\validacion_checkpoint_<jobid>.sqlite3
```

La tabla `results` almacena los resultados por fingerprint. Los writes se realizan en transacciones y SQLite usa WAL + `synchronous=FULL`.

`M3U_STATE_DIR` permite fijar una raíz absoluta alternativa. La aplicación utiliza su subcarpeta `checkpoints`. Un destino existente prevalece incluso vacío. El importador sólo considera el JSON heredado al crear un destino nuevo, valida la identidad y conserva la antigüedad de los resultados. [Configuración y recuperación del estado](docs/CONFIGURATION_AND_RECOVERY.md).

Al reiniciar un job con los mismos inputs/configuración, los resultados persistidos se reutilizan mientras sigan vigentes según su TTL; los vencidos vuelven a validarse.

## Seguridad

La redacción es centralizada y nunca debe propagar credenciales a reportes o diagnósticos. Se eliminan user/password del authority y se redaccionan parámetros sensibles (`token`, `auth`, `api_key`, etc.).

Los checkpoints almacenan URLs redactadas. Consulte los requisitos de red y protección de archivos antes de autorizar un entorno para producción. [Seguridad](SECURITY.md).

El paquete distribuido intencionalmente **no contiene las listas M3U originales ni los JSON de reparación derivados**, porque esos archivos pueden contener credenciales, tokens o endpoints privados.

## Logs y errores

Los errores se clasifican, entre otros, como:

```text
URL_ERROR
URL_UNSUPPORTED
TIMEOUT
FFPROBE_ERROR
NETWORK_ERROR
UNEXPECTED_ERROR
```

Los mensajes de excepción que puedan contener URLs pasan por la redacción antes de escribirse en archivos de error o resultados persistentes.

## Tests

Para ejecutar la suite completa con pytest, prepare el entorno temporal con las versiones fijadas y los perfiles de prueba que se indican en la [guía de instalación](docs/INSTALLATION_V9_0_2.md).

```text
python -B -m pytest -p no:cacheprovider -q
```

El comando `python -B run_self_tests.py` ejecuta los autocontroles independientes y detalla las pruebas que delega a pytest. Entre ambos recorridos se cubren URLs, aislamiento de procesos, generación de informes, reanudación, empaquetado y regresiones funcionales.

## Stress / rendimiento

El harness `tools/stress_harness.py` es de red cero y mide parser + deduplicación + URL gate. En la auditoría se ejecutaron casos sintéticos de 1k, 10k, 50k, 100k y 180k registros.

Estos resultados no equivalen a 180k probes reales de FFprobe. La concurrencia real está además limitada de forma intencional por host para evitar ráfagas.

## Troubleshooting

### FFprobe no encontrado

Instalar FFmpeg, comprobar `ffprobe -version` y revisar el PATH. La validación rápida HTTP/HLS no requiere FFprobe.

### El job debe reanudarse

No borre `validacion_checkpoint_<jobid>.sqlite3`. Vuelva a ejecutar el mismo conjunto de entradas y configuración.

### Un reporte falla después de publicar

El M3U ya publicado se conserva. El sistema intenta escribir `<salida>_informe_fallback.txt` y registra el fallo sin revertir el resultado.

### Hay una URL inválida

El pipeline la clasifica y la manda a cuarentena lógica. No se ejecuta una llamada de red ni FFprobe sobre ella.

## Limitaciones conocidas

1. El modelo actual todavía materializa listas Python de entradas; la auditoría no demuestra 1.000.000 de registros con memoria acotada.
2. El limitador por host (`2` simultáneos y separación de arranque configurada) favorece estabilidad y puede reducir throughput en conjuntos dominados por un mismo host.
3. La verificación visual de la GUI instalada cubre Windows al 150 % real. Las preferencias y checkpoints se guardan por usuario. [Guía de configuración y recuperación](docs/CONFIGURATION_AND_RECOVERY.md).
4. La disponibilidad de streams remotos depende del proveedor y del perfil de salida autorizado.

## Archivos principales

La descripción de los componentes y del recorrido de las entradas está en la [guía de arquitectura](docs/ARCHITECTURE.md).

- `UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE.py`: orquestador y resume.
- `M3U_PRO_V9_ENGINE.py`: parser, normalización, FFprobe y reporting.
- `url_safety.py`: única capa de decisión sintáctica/semántica de URL.
- `checkpoint_store.py`: persistencia durable por fingerprint.
- `runtime_state.py`: ubicación por usuario y recuperación conservadora del estado.
- `M3U_PRO_VALIDATOR.py`: auditoría de archivos M3U usando la misma clasificación URL.
- `M3U_PRO_REPARADOR.py`: reparación conservadora de archivos.
- `enterprise_favorites.py`: favoritos y migración.
- `run_self_tests.py`: runner sin dependencias externas.
- `tools/stress_harness.py`: pruebas sintéticas de escala.

## Prevalidación certificada (10.1)

El flujo opcional es `ORIGINAL → Validator 10.1 → Repairer 10.1 → CLEAN M3U → PREVALIDATION_MANIFEST.json → Unificador Enterprise → FFprobe`.

El Unificador solo activa el modo prevalidado cuando el manifest es compatible y el SHA-256 del M3U limpio coincide. Una ausencia, corrupción, versión incompatible o SHA mismatch provoca fallback automático al comportamiento normal.

`PREVALIDATED` solo certifica sintaxis/clasificación/reparaciones deterministas. Nunca significa `ACTIVE`. FFprobe continúa siendo obligatorio para la validación profunda del stream.

Para usarlo desde la GUI, activa `Usar prevalidación certificada (manifest + SHA-256)` y selecciona los M3U publicados por el Reparador. El modo permanece desactivado por defecto.

### Artefactos de prevalidación

El Reparador publica `PREVALIDATION_MANIFEST.json` en el directorio de salida. El manifiesto identifica el contenido exacto de cada M3U limpio mediante SHA-256. [Contrato de prevalidación](docs/PREVALIDATION_CONTRACT.md).
