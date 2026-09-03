@echo off
chcp 65001 > nul
title Invoice Manager — Panel de Control

echo ========================================================
echo   📄 INVOICE MANAGER — PANEL DE CONTROL Y CALIDAD
echo ========================================================
echo.
echo Iniciando servidor local en segundo plano...
echo.

:: Cambiar al directorio del script
cd /d "%~dp0"

:: Verificar si existe entorno virtual y activarlo
if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
) else if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

:: Abrir el navegador tras 2 segundos
start "" cmd /c "timeout /t 2 /nobreak > nul && start http://127.0.0.1:8000"

:: Iniciar el servidor web con uvicorn y autoreload
python -m uvicorn dashboard.app:app --host 127.0.0.1 --port 8000 --reload
