# Instalar y abrir M3U Enterprise desde el ZIP de fuentes

1. Compruebe el ZIP y el archivo `SBOM.cdx.json` con una copia confiable del verificador. [Verificar la entrega](RELEASE_PACKAGING.md).
2. Extraiga el ZIP en una carpeta nueva. Abra una terminal en la carpeta que contiene `run.py` y `pyproject.toml`.
3. Compruebe que `python` corresponde a Python 3.13 o 3.14 y que puede importar Tkinter.

```powershell
python --version
python -B -c "import tkinter; print(tkinter.TkVersion)"
```

4. Inicie la GUI. La ejecución desde estas fuentes requiere únicamente la biblioteca estándar de Python y Tkinter.

```powershell
python -B run.py
```

En Windows también puede abrir `RUN_ENTERPRISE_V9.bat` en esa carpeta. Para validación profunda, instale FFmpeg con FFprobe disponible en el `PATH` o seleccione el ejecutable con **Buscar FFprobe...** en la GUI. Pulse **Probar** para comprobar su detección. La inspección local de la sintaxis de una lista funciona sin FFprobe. [Primer uso](USAGE_V9_0_2.md).

En Windows, la configuración se guarda por defecto en `%LOCALAPPDATA%\M3UEnterprise\config` y los checkpoints en `%LOCALAPPDATA%\M3UEnterprise\state\checkpoints`. El operador elige el destino de las listas y los informes. [Configuración y recuperación](CONFIGURATION_AND_RECOVERY.md).

## Pruebas opcionales

Para los autocontroles independientes, ejecute `python -B run_self_tests.py` desde una extracción de prueba en una carpeta nueva. El runner informa qué casos delega a pytest.

Para la suite completa en Windows, ejecute estos comandos en una terminal PowerShell nueva, desde la extracción. Se crea un entorno temporal y se aíslan tanto la configuración como el estado. La instalación de herramientas desde PyPI requiere acceso a la red.

```powershell
$lab = Join-Path ([System.IO.Path]::GetTempPath()) ("m3u-pruebas-" + [guid]::NewGuid())
python -m venv (Join-Path $lab 'venv')
$pythonPruebas = Join-Path $lab 'venv\Scripts\python.exe'
$env:M3U_CONFIG_DIR = Join-Path $lab 'config'
$env:M3U_STATE_DIR = Join-Path $lab 'state'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
& $pythonPruebas -m pip --isolated install --index-url https://pypi.org/simple --require-hashes --only-binary=:all: -r requirements-dev.lock
& $pythonPruebas -m pip check
& $pythonPruebas -B -m pytest -p no:cacheprovider -q
```

Si el entorno de pruebas ya dispone de los wheels del lock, sustituya `--index-url https://pypi.org/simple` por `--no-index --find-links "CARPETA_DE_WHEELS"` al instalar. Compruebe el código de salida de cada comando antes de continuar.
