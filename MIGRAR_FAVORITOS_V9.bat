@echo off
setlocal EnableExtensions
cd /d "%~dp0" || exit /b 1
set "runner=py"
where py >nul 2>&1
if errorlevel 1 set "runner=python"
if "%~1"=="" goto personal
%runner% -B FAVORITOS_CATALOGO_ENTERPRISE.py %*
exit /b %errorlevel%
:personal
%runner% -B FAVORITOS_CATALOGO_ENTERPRISE.py --import-legacy .
exit /b %errorlevel%
