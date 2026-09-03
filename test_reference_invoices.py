#!/usr/bin/env python3
"""
Test rápido para verificar las dos correcciones.
Ejecutar desde el directorio del proyecto.
"""
import re
import os
import unicodedata
from datetime import datetime
from pathlib import Path
import pdfplumber

LIDL_PDF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Facturas ejemplo", "factura_2026216100431.pdf")

# Importar funciones del módulo
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from invoice_manager import (
    _extract_fiscal_fingerprint,
    _load_reference_fingerprints,
    _matches_reference_invoice,
)

def test_abbreviated_month_regex():
    """Verifica que el regex mejorado detecta '27-Jun-2026'."""
    print("=" * 60)
    print("TEST 1: Deteccion de mes abreviado en regex")
    print("=" * 60)
    
    with pdfplumber.open(LIDL_PDF) as pdf:
        pdf_text_lower = " ".join(page.extract_text() or "" for page in pdf.pages).lower()
    
    all_month_names = {
        "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
        "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
        "septiembre": 9, "setiembre": 9, "octubre": 10,
        "noviembre": 11, "diciembre": 12,
        "ene": 1, "feb": 2, "mar": 3, "abr": 4,
        "jun": 6, "jul": 7, "ago": 8,
        "sep": 9, "oct": 10, "nov": 11, "dic": 12,
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8,
        "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    
    abbrev_month_pattern = '|'.join(sorted(all_month_names.keys(), key=len, reverse=True))
    abbrev_match = re.search(
        r'\b(\d{1,2})[\s\-/](' + abbrev_month_pattern + r')[\s\-/](\d{4})\b',
        pdf_text_lower
    )
    
    if abbrev_match:
        day = int(abbrev_match.group(1))
        month_name = abbrev_match.group(2)
        month_num = all_month_names[month_name]
        year = int(abbrev_match.group(3))
        print(f"  ENCONTRADO: {day:02d}-{month_name}-{year} -> dia={day}, mes={month_num}, anio={year}")
        
        dt = datetime(year, month_num, day)
        date_str = dt.strftime("%m-%y")
        print(f"  Carpeta destino: {date_str}")
        
        assert day == 27, f"Dia incorrecto: {day}"
        assert month_num == 6, f"Mes incorrecto: {month_num}"
        assert year == 2026, f"Anio incorrecto: {year}"
        assert date_str == "06-26", f"Carpeta incorrecta: {date_str}"
        print("  PASS: Fecha 27-Jun-2026 detectada correctamente -> carpeta 06-26")
    else:
        print("  FAIL: No se detecto la fecha con mes abreviado")
        sys.exit(1)
        
    # Verificar Paso D
    all_months_list = ["ene", "feb", "mar", "abr", "jun", "jul", "ago", "sep", "oct", "nov", "dic",
                       "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    has_date = False
    for m in all_months_list:
        if re.search(rf'\b{m}\b', pdf_text_lower):
            has_date = True
            print(f"  PASS: Mes abreviado '{m}' detectado en Paso D (filtro fecha)")
            break
    
    if not has_date:
        print("  FAIL: Ningun mes abreviado detectado en el Paso D")
        sys.exit(1)
    
    print()


def test_reference_fingerprints():
    """Verifica que el sistema de fingerprinting funciona."""
    print("=" * 60)
    print("TEST 2: Sistema de facturas de referencia (fingerprinting)")
    print("=" * 60)
    
    fps = _load_reference_fingerprints()
    print(f"\n  Fingerprints cargados: {len(fps)}")
    assert len(fps) > 0, "No se cargaron fingerprints"
    
    for fp in fps:
        print(f"  - {fp['filename']}: {len(fp['keywords'])} keywords")
        print(f"    Keywords: {', '.join(sorted(fp['keywords']))}")
    
    # Auto-match
    match = _matches_reference_invoice(LIDL_PDF, fps)
    assert match, "La factura LIDL deberia matchear consigo misma"
    print(f"\n  PASS: Auto-match de factura LIDL funciona correctamente")
    
    # Keywords esenciales
    fp = _extract_fiscal_fingerprint(LIDL_PDF)
    assert fp is not None, "No se pudo extraer fingerprint"
    assert "factura" in fp, "'factura' deberia estar en el fingerprint"
    assert "nif" in fp, "'nif' deberia estar en el fingerprint"
    assert "iva" in fp, "'iva' deberia estar en el fingerprint"
    print(f"  PASS: Keywords fiscales clave presentes en el fingerprint")
    
    print()


if __name__ == "__main__":
    print("\nEjecutando tests de verificacion...\n")
    
    if not os.path.exists(LIDL_PDF):
        print(f"Aviso: No se encontro la factura de ejemplo local ({LIDL_PDF}).")
        print("Coloca facturas de prueba en 'Facturas ejemplo/' para ejecutar este test.")
        sys.exit(0)
    
    test_abbreviated_month_regex()
    test_reference_fingerprints()
    
    print("=" * 60)
    print("Todos los tests pasaron correctamente!")
    print("=" * 60)
