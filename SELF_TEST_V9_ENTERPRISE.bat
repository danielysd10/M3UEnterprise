@echo off
setlocal EnableExtensions
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 goto use_py
where python >nul 2>&1
if not errorlevel 1 goto use_python
where python3 >nul 2>&1
if not errorlevel 1 goto use_python3
echo ERROR: No se encontro Python 3 en el PATH.
echo Instala Python 3.13 o 3.14, conforme a pyproject.toml, y agrega python.exe al PATH para ejecutar las pruebas.
set "RC=2"
goto finish
:use_py
py run_self_tests.py
set "RC=%errorlevel%"
goto finish
:use_python
python run_self_tests.py
set "RC=%errorlevel%"
goto finish
:use_python3
python3 run_self_tests.py
set "RC=%errorlevel%"
:finish
echo.
if "%RC%"=="0" (echo SELF TEST FINALIZADO CORRECTAMENTE.) else (echo SELF TEST FINALIZADO CON ERRORES. Codigo=%RC%)
exit /b %RC%
