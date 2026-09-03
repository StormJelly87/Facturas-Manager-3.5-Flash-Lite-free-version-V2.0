import re
from datetime import datetime
from dateutil import parser as dateutil_parser

def process_date(pdf_text_lower, raw_date_from_ia, current_date_str="2026-03-18"):
    # === REPRODUCCIÓN EXACTA DE INVOICE_MANAGER.PY ===
    forced_date = None
    forced_month_num = None
    forced_day_num = None
    
    # 1. Regex Estricto (ej. 06/03/2026, 6-3-2026)
    match = re.search(r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b', pdf_text_lower)
    if match:
        d = int(match.group(1))
        m = int(match.group(2))
        y = int(match.group(3))
        if 1 <= d <= 31 and 1 <= m <= 12:
            forced_date = datetime(y, m, d)
    
    # 2. Mapeo Matemático de Meses
    if not forced_date:
        spanish_months = {
            "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
            "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
            "septiembre": 9, "setiembre": 9, "octubre": 10,
            "noviembre": 11, "diciembre": 12
        }
        month_match = re.search(r'\b(\d{1,2})\s+(?:de\s+)?(' + '|'.join(spanish_months.keys()) + r')\b', pdf_text_lower)
        if month_match:
            forced_day_num = int(month_match.group(1))
            forced_month_num = spanish_months[month_match.group(2)]
        else:
            for m_name, m_num in spanish_months.items():
                if re.search(rf'\b{m_name}\b', pdf_text_lower):
                    forced_month_num = m_num
                    break

    # ── PASO C: Parsear fecha (formato primario ISO YYYY-MM-DD, fallback europeo) ──
    try:
        if raw_date_from_ia:
            try:
                dt = datetime.strptime(raw_date_from_ia, "%Y-%m-%d")
            except ValueError:
                dt = dateutil_parser.parse(raw_date_from_ia, dayfirst=True)
        else:
            dt = datetime.strptime(current_date_str, "%Y-%m-%d")
    except (ValueError, OverflowError):
        dt = datetime.strptime(current_date_str, "%Y-%m-%d")

    # Aplicar Reglas de Verificacion y Mapeo:
    if forced_date:
        dt = forced_date
    elif forced_month_num is not None:
        if forced_day_num is not None:
            try:
                dt = dt.replace(month=forced_month_num, day=forced_day_num)
            except ValueError:
                dt = dt.replace(month=forced_month_num)
        else:
            try:
                dt = dt.replace(month=forced_month_num)
            except ValueError:
                pass

    # Filtro de Seguridad Automático (Fecha Futura)
    now = datetime.strptime(current_date_str, "%Y-%m-%d")
    if dt > now:
        try:
            dt = dt.replace(month=dt.day, day=dt.month)
        except ValueError:
            pass
        
        if dt > now:
            dt = now

    return dt.strftime("%Y-%m-%d"), dt.strftime("%m-%y")

# ─── BATERÍA DE TESTS RIGUROSOS (ESPAÑA DD/MM/YYYY) ───

tests = [
    # (Texto PDF, Respuesta IA, Esperado ISO, Explicación del caso)
    
    # 1. IA perfecta, ISO estándar YYYY-MM-DD
    ("texto sin fechas", "2026-03-06", "2026-03-06", "IA da fecha perfecta ISO"),
    
    # 2. IA se confunde y devuelve algo europeo sin ISO. Regex lo pilla en el PDF
    ("fecha factura: 12/03/2026 en valencia", "03-12-2026", "2026-03-12", "Regex estricto pisa toda IA y usa DD/MM/YYYY"),
    
    # 3. Mapeo textual: El PDF dice '6 de marzo', la IA se inventa el formato
    ("madrid, 6 de marzo de 2026", "2026-06-03", "2026-03-06", "Mapeo textual (6 de marzo) anula a IA"),
    
    # 4. Solo el mes está en el texto ("marzo") y la IA dice "2026-06-03" (esperamos que el mes se fuerce a 3, pero se respete el día de IA)
    ("facturacion correspondiente al mes de marzo.", "2026-06-06", "2026-03-06", "Caída a forzar mes textualmente a marzo (03). IA dice día 06, así que 06/03"),
    
    # 5. La IA falla y no devuelve nada, el script encuentra un formato con guiones
    ("factura numero 1928, fecha 15-08-2025", "", "2025-08-15", "Rescatando fecha completa 15-08-2025 del OCR"),
    
    # 6. Futuro loco que no se arregla invirtiendo (ej. IA dice 2026-10-15 pero estamos en marzo. Y PDF no dice nada)
    ("documento vacio", "2026-10-15", "2026-03-18", "Fecha en el futuro, se invierte a 2026-15-10 (inválido), vuelve a la fecha de hoy"),
    
    # 7. Futuro que SÍ se arregla invirtiendo (IA dice 2026-12-03, invierte a 2026-03-12)
    ("documento en ingles invoice", "2026-12-03", "2026-03-12", "Filtro de seguridad invirtiendo para arreglar mes y día en americano"),
    
    # 8. Un caso como 29 de febrero bisiesto
    ("madrid, 29 de febrero de 2024", "2024-02-29", "2024-02-29", "Soporta bisiestos correctamente"),
]

print("="*60)
print("INICIANDO TESTS AVANZADOS PARA ESPAÑA (DD/MM/YYYY)")
print("="*60)

for pdf, ia, expected, desc in tests:
    result_iso, folder = process_date(pdf, ia)
    status = "✅ PASS" if result_iso == expected else "❌ FAIL"
    print(f"{status} | {desc}")
    print(f"      PDF: '{pdf[:30]}...' -> IA: '{ia}'")
    print(f"      Resultado final: {result_iso} (Carpeta Drive: {folder})")
    print("-" * 60)
