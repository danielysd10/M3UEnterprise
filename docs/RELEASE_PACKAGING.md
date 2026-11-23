# Límite de distribución y verificación

El release de fuentes se construye a partir de rutas exactas revisadas en tools/release_policy.py. No se recorre el árbol completo ni se usa .gitignore como barrera de publicación. Un archivo nuevo no se distribuye hasta revisar su contenido, consumidores y necesidad y añadir su ruta a la política.

Se excluyen datos locales, playlists, registros privados, inventarios de datos, secretos, checkpoints, cachés y archivos desconocidos. La política valida nombres portables, rechaza enlaces simbólicos, junctions/puntos de reanálisis y hardlinks de fuentes. Requiere que todas las fuentes aprobadas existan. Esto no sustituye revisar secretos incrustados en archivos fuente aprobados.

En el Windows de laboratorio, el error 1314 impidió crear enlaces simbólicos para comprobar cuatro ubicaciones: archivo de entrada, directorio intermedio, raíz de fuentes y directorio de salida. Use rutas ordinarias para empaquetar en el entorno verificado. Si una entrega exige acreditar el rechazo de esos enlaces, ejecute los cuatro casos de `test_symlink_boundaries_fail_closed` en un laboratorio con el privilegio correspondiente. [Pruebas de empaquetado](../tests/test_release_boundary.py).

## Construcción

Desde la raíz del proyecto:

~~~powershell
python -B tools/package_release.py --output-dir artifacts/releases/candidate-001
~~~

Use una carpeta nueva fuera del árbol de fuentes o bajo `artifacts/`. El empaquetador prepara el ZIP en un subdirectorio temporal de la salida y verifica su manifiesto. A continuación crea `SBOM.cdx.json` en exclusiva, lo cierra y comprueba su hash. Todos los empaquetadores que apuntan a esa carpeta compiten por ese nombre, incluso si construyen versiones distintas. El ganador hace visible el ZIP completo en una sola operación que respeta cualquier archivo existente en el destino.

En Windows se usa un cambio de nombre en el mismo volumen. En POSIX la operación requiere un sistema de archivos con soporte para enlaces físicos. Si falla después de crear el SBOM, la salida queda incompleta. La prueba de terminación de procesos corresponde a un disco local en Windows. La durabilidad ante un corte eléctrico y el comportamiento en carpetas de red quedan fuera del alcance probado.

El nombre del ZIP mantiene el formato anterior para compatibilidad; el texto PRODUCTION_READY del nombre no certifica operación productiva. La versión del producto permanece en pyproject.toml hasta una decisión de release.

## Verificación

~~~powershell
python -B tools/verify_release.py --zip RUTA_AL_ZIP --sbom RUTA_AL_SBOM
~~~

Use el verificador de una fuente confiable. Con ambos argumentos comprueba el manifiesto completo y compara el SHA-256 del SBOM externo con el del SBOM incluido en el ZIP. Devuelve código 0 y muestra `release_sha256` y `release_sbom_sha256` cuando la entrega es válida. Puede contrastar esos valores con los emitidos por el empaquetador. La opción `--zip` por sí sola sigue disponible para examinar un ZIP individual.

| Qué hay en la carpeta de salida | Qué hacer |
|---|---|
| Faltan el ZIP o el SBOM, o sólo existe un directorio temporal `.release-*` | El intento está en curso o quedó incompleto. Si el proceso terminó, conserve la carpeta y reintente con otra salida nueva. |
| Existen los dos archivos | Ejecute el verificador con ambas rutas. Distribuya el par únicamente si devuelve 0. |
| El SBOM está vacío, los hashes difieren o el verificador devuelve otro error | Conserve los archivos para diagnóstico y vuelva a construir en una carpeta nueva. |

Para repetir el ejemplo anterior, cambie `candidate-001` por `candidate-002` y verifique el par de esa carpeta. Conserve cualquier archivo ajeno del primer intento.

El verificador exige una sola raíz, el conjunto exacto de archivos, nombres portables, tipos regulares y un manifiesto sin duplicados. Comprueba los tamaños y calcula los hashes del contenido por bloques.

El manifiesto permite detectar cambios en los archivos. Para comprobar también su procedencia, obtenga de una fuente confiable el repositorio, el commit y la referencia aprobados para esa entrega. Use el verificador del corte para validar primero el ZIP junto al SBOM recibido.

## Comprobar una entrega firmada de GitHub

En el resumen de la ejecución autorizada busque las dos declaraciones del mismo ZIP. Una contiene la procedencia SLSA y la otra contiene el inventario CycloneDX. El trabajo de firma descarga el artefacto de la versión de Python correspondiente, verifica los dos bundles que acaba de emitir y compara el inventario firmado con `SBOM.cdx.json`.

Para verificar el archivo recibido con GitHub CLI, sustituya los valores de ejemplo por el ZIP, repositorio, commit completo y referencia de la entrega aprobada. La referencia debe incluir el prefijo Git completo. Use una versión de `gh` que admita los filtros del ejemplo y una sesión con acceso de lectura al repositorio. [Manual de verificación de GitHub CLI](https://cli.github.com/manual/gh_attestation_verify).

~~~powershell
$zip = 'RUTA_AL_ZIP'
$repo = 'ORGANIZACION/REPOSITORIO'
$commit = 'SHA_COMPLETO_DEL_COMMIT'
$ref = 'refs/heads/RAMA_APROBADA'
$origen = @('--repo', $repo, '--signer-workflow', "$repo/.github/workflows/ci.yml",
            '--source-digest', $commit, '--source-ref', $ref)
gh attestation verify $zip @origen --predicate-type https://slsa.dev/provenance/v1
if ($LASTEXITCODE -ne 0) { throw 'No se verificó la procedencia' }
gh attestation verify $zip @origen --predicate-type https://cyclonedx.org/bom
if ($LASTEXITCODE -ne 0) { throw 'No se verificó la declaración del SBOM' }
~~~

Conserve los SHA-256 del par y el identificador de la ejecución cuyas declaraciones se verificaron. La generación de un par local permite comprobar su integridad. La firma y la comprobación de procedencia alojadas requieren una ejecución satisfactoria del trabajo `attest`.

Límites actuales: máximo 16 MiB por archivo y 64 MiB por release; manifiesto máximo 256 KiB. Cualquier cambio deliberado requiere revisión y pruebas. Las comprobaciones de archivos presuponen un checkout estable durante el empaquetado; no son aislamiento frente a un proceso hostil que controle simultáneamente el equipo.

## Compatibilidad

- Puede construir el paquete desde el repositorio o desde un ZIP de fuentes verificado y extraído. La lista exacta de rutas forma parte de la política del verificador. Para comprobar una entrega anterior, use una copia confiable del verificador y la política que correspondan a ese corte. La versión que figura en el nombre del ZIP por sí sola no identifica la revisión de esa política.
- El trabajo de validación comprueba el par antes de cargarlo como artefacto de CI. El trabajo de firma sólo se ejecuta después de validar ambas versiones de Python, en un push o despacho manual sobre una referencia protegida.
- En el ZIP extraído, `SHA256SUMS_OPTIMIZED.txt` y `SBOM.cdx.json` describen el paquete recibido. Los archivos homónimos del checkout original registran una entrega histórica.
- El ZIP incluye guías, ejemplos y herramientas. El wheel instala los módulos Python y los puntos de entrada CLI y GUI. Los catálogos y metadatos del wheel se proporcionan como configuración explícita. Las preferencias y los checkpoints se guardan en el perfil del usuario. [Instalación](INSTALLATION_V9_0_2.md) · [Configuración y recuperación](CONFIGURATION_AND_RECOVERY.md).

Los expedientes de auditoría y los documentos de continuidad se conservan en el repositorio de desarrollo. El lector del ZIP puede iniciar la instalación, el uso y la verificación desde el [manual del producto](../README.md).

## Referencias técnicas

El formato del ZIP y su verificación utilizan la biblioteca estándar de Python. [zipfile](https://docs.python.org/3.14/library/zipfile.html) · [pathlib](https://docs.python.org/3.14/library/pathlib.html) · [os.path](https://docs.python.org/3.14/library/os.path.html).

El SBOM mínimo describe el runtime con CycloneDX 1.5 y utiliza un UUID derivado de su contenido. Para las herramientas de desarrollo, consulte el archivo de dependencias fijadas. [CycloneDX 1.5](https://cyclonedx.org/docs/1.5/json/#serialNumber) · [Lock de herramientas](../requirements-dev.lock).
