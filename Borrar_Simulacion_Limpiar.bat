@echo off
chcp 65001 > nul
title Limpiar Simulación y Restaurar Datos Reales

echo ========================================================
echo   🧹 LIMPIEZA Y RESTAURACIÓN DE DATOS REALES
echo ========================================================
echo.
echo Eliminando todas las facturas de prueba y restaurando tu estado original...
echo.

cd /d "%~dp0"
python simulacion_manager.py --restore

echo.
echo ========================================================
echo   ✅ LISTO. Todos los datos de prueba han sido borrados
echo   y tus datos reales están restaurados al 100%.
echo   Recarga tu navegador (F5) para comprobarlo.
echo ========================================================
echo.
pause
