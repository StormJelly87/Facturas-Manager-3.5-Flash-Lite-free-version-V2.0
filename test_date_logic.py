import re
from datetime import datetime
from dateutil import parser as dateutil_parser

def test_date_logic(pdf_text_lower, raw_date, original_date_str="2026-03-18"):
    print(f"\n--- Probando con texto de PDF: '{pdf_text_lower}' | Fecha IA: '{raw_date}' ---")
    
    # === Lógica de Validación Numérica y de Contexto ===
    forced_date = None
    forced_month_num = None
    forced_day_num = None
    
    # 1. Regex Estricto
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

    # ── PASO C: Parsear fecha (simulación) ──
    try:
        if raw_date:
            dt = dateutil_parser.parse(raw_date, dayfirst=True)
        else:
            dt = datetime.strptime(original_date_str, "%Y-%m-%d")
    except (ValueError, OverflowError):
        dt = datetime.strptime(original_date_str, "%Y-%m-%d")

    # Aplicar Reglas de Verificacion y Mapeo:
    if forced_date:
        dt = forced_date
        print(f"  [VERIFICACIÓN-NUMÉRICA] Día: {dt.day:02d}, Mes: {dt.month:02d}. Confirmado por Regex Estricto.")
    elif forced_month_num is not None:
        log_str = "Confirmado por contexto español."
        if forced_day_num is not None:
            try:
                dt = dt.replace(month=forced_month_num, day=forced_day_num)
                print(f"  [VERIFICACIÓN-NUMÉRICA] Día: {forced_day_num:02d}, Mes: {forced_month_num:02d}. {log_str}")
            except ValueError:
                dt = dt.replace(month=forced_month_num)
                print(f"  [VERIFICACIÓN-NUMÉRICA] Día: {dt.day:02d}, Mes: {forced_month_num:02d}. {log_str}")
        else:
            try:
                dt = dt.replace(month=forced_month_num)
                print(f"  [VERIFICACIÓN-NUMÉRICA] Día: {dt.day:02d}, Mes: {forced_month_num:02d}. {log_str}")
            except ValueError:
                pass

    # Filtro de Seguridad Automático (Fecha Futura)
    now = datetime.strptime(original_date_str, "%Y-%m-%d") # Mockeamos la fecha actual al 18 de marzo
    if dt > now:
        print(f"  [SEGURIDAD] Fecha futura detectada ({dt.strftime('%Y-%m-%d')}). Invirtiendo día y mes automáticamente.")
        try:
            dt = dt.replace(month=dt.day, day=dt.month)
        except ValueError:
            pass
        
        if dt > now:
            dt = now

    date_str = dt.strftime("%m-%y")

    # Doble comprobación: log del día, mes y carpeta destino
    print(f"  [FECHA] Procesando dia: {dt.strftime('%d')}, mes: {dt.strftime('%m')}. Carpeta destino: {date_str}")
    return date_str

if __name__ == '__main__':
    # Caso 1: La IA clasifica mal 06/03/2026 como junio (03/06/2026), 
    # pero nuestro Regex Estricto detecta "06/03/2026"
    test_date_logic("factura es6fa8oabei fecha: 06/03/2026 total 100", "2026-06-03")

    # Caso 2: El PDF dice "6 de marzo 2026". La IA dice "2026-06-03" (asume que 6 es el mes, y 3 es marzo? o simplemente falla)
    test_date_logic("factura emitida el 6 de marzo por servicios", "2026-06-03")

    # Caso 3: Inteligencia artificial devuelve una fecha futura y no hay texto legible en PDF
    # Mock estamos al "2026-03-18", y la IA devuelve "2026-06-03" (junio)
    test_date_logic("factura escaneada (sin texto OCR detectable)", "2026-06-03")

    # Caso 4: Inteligencia artificial devuelve fecha correcta y el regex la confirma
    test_date_logic("fecha: 10-02-2026", "2026-02-10")
