@echo off
chcp 65001 > nul
title Generar Simulación de Facturas — Test Visual

echo ========================================================
echo   🧪 GENERADOR DE SIMULACIÓN (70 FACTURAS DE PRUEBA)
echo ========================================================
echo.
echo Creando copia de seguridad y cargando 70 facturas de prueba...
echo.

cd /d "%~dp0"
python simulacion_manager.py

echo.
echo ========================================================
echo   ✅ LISTO. Recarga la página en tu navegador (F5)
echo   para ver el panel completo en funcionamiento.
echo ========================================================
echo.
pause
