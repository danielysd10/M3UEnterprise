@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if "%~1"=="" (
  echo Uso: REPARAR_M3U_V9.bat archivo.m3u [otro.m3u ...] [-o directorio] [--strict] [--no-backup]
  pause
  exit /b 2
)
where py >nul 2>&1
if errorlevel 1 goto use_python
py M3U_PRO_REPARADOR.py %*
exit /b %errorlevel%
:use_python
python M3U_PRO_REPARADOR.py %*
exit /b %errorlevel%
