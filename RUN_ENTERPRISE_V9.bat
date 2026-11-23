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
echo Instala Python 3.13 o 3.14 con Tkinter, conforme a pyproject.toml, y agrega python.exe al PATH.
pause
exit /b 2
:use_py
py UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE.py
exit /b %errorlevel%
:use_python
python UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE.py
exit /b %errorlevel%
:use_python3
python3 UNIFICADOR_M3U_PRO_V9_0_0_ENTERPRISE.py
exit /b %errorlevel%
