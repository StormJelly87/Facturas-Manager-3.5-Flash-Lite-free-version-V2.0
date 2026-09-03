"""
simulacion_manager.py — Generador y Restaurador de Simulación Visual de Facturas.

Permite:
1. Crear una copia de seguridad exacta de tus datos reales actuales en data/backup_real_...
2. Inyectar 70 facturas realistas distribuidas en varios meses (2025 y 2026) y proveedores variados:
   - 60 Procesadas con Éxito (con sus rutas jerárquicas exactas en Google Drive).
   - 5 Facturas Dudosas con fecha ambigua para probar las opciones A y B o descarte.
   - 5 Facturas Descartadas (albaranes, pedidos, etc.) con sus PDFs de muestra.
   - 3 Reglas memorizadas explicadas con detalle.
   - Resumen visual en el Panel de Control y registros en el terminal.
3. Restaurar en 1 segundo tus datos originales dejando el sistema 100% limpio.
"""

import os
import sys
import json
import shutil
import random
from datetime import datetime, timedelta

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
QUARANTINE_DIR = os.path.join(DATA_DIR, "quarantine")
HISTORY_FILE = os.path.join(DATA_DIR, "invoices_history.json")
RULES_FILE = os.path.join(DATA_DIR, "vendor_rules.json")
SCHEDULE_FILE = os.path.join(DATA_DIR, "schedule_config.json")

BACKUP_HISTORY = os.path.join(DATA_DIR, "backup_real_invoices_history.json")
BACKUP_RULES = os.path.join(DATA_DIR, "backup_real_vendor_rules.json")
BACKUP_SCHEDULE = os.path.join(DATA_DIR, "backup_real_schedule_config.json")


def make_sample_pdf(title: str, supplier: str, date_str: str, amount_str: str, note: str = "") -> bytes:
    """Genera un archivo PDF válido y ligero para que el visor lo renderice perfectamente."""
    stream_content = (
        f"BT "
        f"/F1 18 Tf 50 720 Td ({title}) Tj "
        f"/F1 12 Tf 0 -35 Td (PROVEEDOR: {supplier}) Tj "
        f"0 -25 Td (FECHA: {date_str}) Tj "
        f"0 -25 Td (TOTAL DOCUMENTO: {amount_str} EUR) Tj "
        f"0 -30 Td (DETALLE: {note}) Tj "
        f"/F1 10 Tf 0 -45 Td (Documento generado para simulacion de control de calidad) Tj "
        f"ET"
    )
    stream_len = len(stream_content.encode("latin1", errors="replace"))
    pdf = f"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj
4 0 obj << /Length {stream_len} >>
stream
{stream_content}
endstream
endobj
5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000056 00000 n 
0000000111 00000 n 
0000000240 00000 n 
0000000350 00000 n 
trailer << /Size 6 /Root 1 0 R >>
startxref
420
%%EOF
"""
    return pdf.encode("latin1", errors="replace")


def generar_simulacion():
    """Crea la simulación completa de 70 facturas respaldando previamente los datos reales."""
    os.makedirs(QUARANTINE_DIR, exist_ok=True)

    # 1. Copia de seguridad si no existe ya
    if not os.path.exists(BACKUP_HISTORY) and os.path.exists(HISTORY_FILE):
        shutil.copy2(HISTORY_FILE, BACKUP_HISTORY)
        print("  [BACKUP] Copia de seguridad creada: backup_real_invoices_history.json")

    if not os.path.exists(BACKUP_RULES) and os.path.exists(RULES_FILE):
        shutil.copy2(RULES_FILE, BACKUP_RULES)
        print("  [BACKUP] Copia de seguridad creada: backup_real_vendor_rules.json")

    # Proveedores realistas
    PROVEEDORES_VALIDOS = [
        "IBERDROLA CLIENTES S.A.U.",
        "ENDESA ENERGÍA XXI",
        "VODAFONE ESPAÑA S.A.U.",
        "TELEFÓNICA DE ESPAÑA",
        "AMAZON EU SARL",
        "WURTH ESPAÑA S.A.",
        "MAKRO DISTRIBUCIÓN",
        "LEROY MERLIN S.L.",
        "MERCADONA S.A.",
        "DHL EXPRESS SPAIN",
        "SEUR GEOPOS",
        "TITANIA TOOLS (ENVÍOMÉDICAL)",
        "MICROSOFT IRELAND OPERATIONS",
        "GOOGLE CLOUD EMEA",
        "REPSOL DIRECTO S.A.",
        "CANAL DE ISABEL II",
        "PROCLINIC S.A.",
        "HENRY SCHEIN ESPAÑA",
        "SUMINISTROS DENTALES BCN",
        "CARREFOUR ESPAÑA",
    ]

    MESES_2025 = ["10-25", "11-25", "12-25"]
    MESES_2026 = ["01-26", "02-26", "03-26", "04-26", "05-26", "06-26", "07-26", "08-26", "09-26"]
    TODOS_MESES = MESES_2025 + MESES_2026

    simulated_history = []

    # ── 60 FACTURAS CON ÉXITO ────────────────────────────────────────────────
    print("  [GENERANDO] Creando 60 facturas procesadas con éxito...")
    for i in range(1, 61):
        supplier = random.choice(PROVEEDORES_VALIDOS)
        month_str = random.choice(TODOS_MESES)
        year_full = f"20{month_str.split('-')[1]}"
        amount = round(random.uniform(18.50, 1420.00), 2)
        invoice_num = f"{random.randint(1000, 9999)}/{month_str.split('-')[1]}"
        filename = f"Factura_{supplier.split()[0]}_{invoice_num.replace('/', '_')}.pdf"

        # Fecha de creación simulada escalonada
        day_created = random.randint(1, 28)
        created_dt = f"{year_full}-{month_str.split('-')[0]}-{day_created:02d}T{random.randint(8,18):02d}:{random.randint(10,59):02d}:00"

        entry_id = f"sim_{year_full}{month_str.split('-')[0]}_{i:03d}"
        simulated_history.append({
            "id": entry_id,
            "status": "SUCCESS",
            "filename": filename,
            "supplier": supplier,
            "date_str": month_str,
            "drive_file_id": f"drive_sim_file_{i:04d}",
            "drive_folder_id": f"drive_folder_prov_{i:04d}",
            "drive_folder_path": f"{year_full} / {month_str} / {supplier}",
            "reason": "Factura fiscal válida clasificada por Gemini 3.5 Flash-Lite",
            "quarantine_file": None,
            "email_subject": f"Factura electrónica nº {invoice_num} - {supplier}",
            "sender": f"facturas@{supplier.split()[0].lower()}.com",
            "detected_dates": [],
            "amount": amount,
            "created_at": created_dt,
            "updated_at": created_dt,
        })

    # ── 5 FACTURAS DUDOSAS (AMBIGÜEDAD DE FECHA DÍA/MES ≤ 12) ─────────────────
    print("  [GENERANDO] Creando 5 facturas dudosas con opciones dinámicas y archivo de muestra...")
    DUDOSAS_DATA = [
        ("GLOBAL TOOLS SUPPLIES UK", "invoice_GT_0405.pdf", 4, 5, 2026, "4 de Mayo de 2026", "5 de Abril de 2026", 185.00),
        ("DENTAL DIRECT GMBH", "rechnung_DD_0607.pdf", 6, 7, 2026, "6 de Julio de 2026", "7 de Junio de 2026", 420.50),
        ("BIO-LAB INSTRUMENTS INT", "bill_BL_0209.pdf", 2, 9, 2026, "2 de Septiembre de 2026", "9 de Febrero de 2026", 890.00),
        ("FAST LOGISTICS SERVICES", "tax_invoice_0811.pdf", 8, 11, 2026, "8 de Noviembre de 2026", "11 de Agosto de 2026", 64.20),
        ("PRECISION MEDICAL CO", "inv_PM_0310.pdf", 3, 10, 2026, "3 de Octubre de 2026", "10 de Marzo de 2026", 310.75),
    ]

    for i, (supplier, filename, d, m, y, label1, label2, amount) in enumerate(DUDOSAS_DATA, 1):
        entry_id = f"sim_dudosa_{i:02d}"
        q_file = f"{entry_id}.pdf"
        q_path = os.path.join(QUARANTINE_DIR, q_file)

        pdf_bytes = make_sample_pdf("FACTURA EMITIDA (FECHA AMBIGUA)", supplier, f"{d:02d}/{m:02d}/{y}", str(amount), "Fecha con dia y mes <= 12 sin regla previa")
        with open(q_path, "wb") as f:
            f.write(pdf_bytes)

        month_folder = f"{m:02d}-{str(y)[-2:]}"
        simulated_history.append({
            "id": entry_id,
            "status": "AMBIGUOUS_DATE",
            "filename": filename,
            "supplier": supplier,
            "date_str": month_folder,
            "drive_file_id": f"drive_prov_{entry_id}",
            "drive_folder_id": f"folder_prov_{entry_id}",
            "drive_folder_path": f"{y} / {month_folder} / {supplier}",
            "reason": f"Ambigüedad de fecha detectada: tanto {d} como {m} son menores o iguales a 12",
            "quarantine_file": q_file,
            "email_subject": f"Invoice attachment {filename}",
            "sender": f"billing@{supplier.split()[0].lower()}.com",
            "detected_dates": [
                {"day": d, "month": m, "year": y, "format": "DD/MM/YYYY", "label": label1},
                {"day": m, "month": d, "year": y, "format": "MM/DD/YYYY", "label": label2},
            ],
            "amount": amount,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })

    # ── 5 DOCUMENTOS DESCARTADOS (PARA PROBAR RESCATE O DESCARTE) ─────────────
    print("  [GENERANDO] Creando 5 documentos descartados con archivos de muestra...")
    DESCARTADAS_DATA = [
        ("CLIENTES CARREFOUR", "confirmacion_pedido_1339281.pdf", "Confirmación de pedido nº 1339281 (sin CIF ni valor de factura legal)", "Pedido de compras"),
        ("SEUR TRANSPORTE", "albaran_entrega_SEUR_9941.pdf", "Albarán de entrega sin desglose fiscal ni NIF emisor", "Albarán de recepción"),
        ("LEROY MERLIN PRESUPUESTOS", "presupuesto_reforma_LM_88.pdf", "Presupuesto proforma informativo no vinculante", "Presupuesto informativo"),
        ("NEWSLETTER PROVEEDORES", "catalogo_ofertas_septiembre.pdf", "Publicidad comercial y catálogo de ofertas mensual", "Boletín comercial"),
        ("HOTEL RESORT BCN", "reserva_confirmada_HR881.pdf", "Confirmación de reserva hotelera pendiente de facturación", "Reserva de viaje"),
    ]

    for i, (supplier, filename, reason, detail) in enumerate(DESCARTADAS_DATA, 1):
        entry_id = f"sim_descartada_{i:02d}"
        q_file = f"{entry_id}.pdf"
        q_path = os.path.join(QUARANTINE_DIR, q_file)

        pdf_bytes = make_sample_pdf("DOCUMENTO NO FISCAL (DESCARTADO)", supplier, "03/09/2026", "0.00", detail)
        with open(q_path, "wb") as f:
            f.write(pdf_bytes)

        simulated_history.append({
            "id": entry_id,
            "status": "DISCARDED",
            "filename": filename,
            "supplier": supplier,
            "date_str": "09-26",
            "drive_file_id": None,
            "drive_folder_id": None,
            "drive_folder_path": None,
            "reason": reason,
            "quarantine_file": q_file,
            "email_subject": f"Envío de documento: {filename}",
            "sender": f"avisos@{supplier.split()[0].lower()}.com",
            "detected_dates": [],
            "amount": None,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })

    # Guardar historial simulado
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(simulated_history, f, indent=2, ensure_ascii=False)

    # ── 3 REGLAS MEMORIZADAS REALISTAS ───────────────────────────────────────
    print("  [GENERANDO] Creando 3 reglas aprendidas explicadas...")
    simulated_rules = {
        "rules": {
            "AMAZON EU SARL": {
                "display_name": "AMAZON EU SARL",
                "date_format": "DD/MM/YYYY",
                "always_accept": True,
                "trusted_senders": ["auto-confirm@amazon.es", "facturas@amazon.com"],
                "tax_id": "ESA00000000",
                "notes": "Facturas emitidas desde la central europea",
                "learned_from": "rescate_manual",
                "origin_document": "Amazon_INV_ES_991823.pdf",
                "user_reason": "Facturas válidas con NIF intracomunitario",
                "human_explanation": "Aprendida tras rescatar manualmente una factura de compra electrónica en 'Amazon_INV_ES_991823.pdf'. Motivo indicado: \"Facturas válidas con NIF intracomunitario\". A partir de ahora el sistema forzará su aceptación.",
                "created_at": "2026-08-15T11:20:00",
                "updated_at": "2026-08-15T11:20:00",
            },
            "WURTH ESPANA S A": {
                "display_name": "WURTH ESPAÑA S.A.",
                "date_format": "DD/MM/YYYY",
                "always_accept": False,
                "trusted_senders": ["facturacion@wurth.es"],
                "tax_id": "A08249822",
                "notes": "Proveedor habitual de tornillería y taller",
                "learned_from": "resolucion_ambiguedad",
                "origin_document": "Factura_W2026_09441.pdf",
                "user_reason": "",
                "human_explanation": "Aprendida al resolver una fecha ambigua en el documento 'Factura_W2026_09441.pdf'. Confirmaste que el proveedor emite en formato DD/MM/YYYY.",
                "created_at": "2026-08-20T14:45:00",
                "updated_at": "2026-08-20T14:45:00",
            },
            "IBERDROLA CLIENTES S A U": {
                "display_name": "IBERDROLA CLIENTES S.A.U.",
                "date_format": "DD/MM/YYYY",
                "always_accept": True,
                "trusted_senders": ["facturaelectronica@iberdrola.es"],
                "tax_id": "A95748356",
                "notes": "Factura de suministro eléctrico de clínica",
                "learned_from": "configuracion",
                "origin_document": "",
                "user_reason": "",
                "human_explanation": "Proveedor habitual de suministros eléctricos. El sistema procesará sus facturas de luz con formato DD/MM/YYYY automáticamente.",
                "created_at": "2026-07-01T09:00:00",
                "updated_at": "2026-07-01T09:00:00",
            },
        }
    }

    with open(RULES_FILE, "w", encoding="utf-8") as f:
        json.dump(simulated_rules, f, indent=2, ensure_ascii=False)

    print("\n  ✅ ¡SIMULACIÓN GENERADA CON ÉXITO!")
    print(f"  • Total facturas inyectadas: {len(simulated_history)}")
    print("  • Procesadas con éxito: 60 (repartidas entre 2025 y 2026)")
    print("  • Facturas Dudosas: 5 (con opciones dinámicas listas para interactuar)")
    print("  • Facturas Descartadas: 5 (con visores PDF reales y botón de confirmación)")
    print("  • Reglas memorizadas: 3 (con explicaciones detalladas)")
    print("  • Copia de seguridad guardada en data/backup_real_...")


def restaurar_datos_reales():
    """Elimina toda la simulación y restaura los datos limpios originales."""
    print("  [RESTAURANDO] Limpiando simulación y restaurando datos reales...")

    # 1. Restaurar historial
    if os.path.exists(BACKUP_HISTORY):
        shutil.copy2(BACKUP_HISTORY, HISTORY_FILE)
        os.remove(BACKUP_HISTORY)
        print("  • Historial real restaurado.")
    else:
        # Si no había backup, dejar lista vacía o limpia
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump([], f, indent=2)
        print("  • Historial reseteado a limpio.")

    # 2. Restaurar reglas
    if os.path.exists(BACKUP_RULES):
        shutil.copy2(BACKUP_RULES, RULES_FILE)
        os.remove(BACKUP_RULES)
        print("  • Reglas reales restauradas.")
    else:
        with open(RULES_FILE, "w", encoding="utf-8") as f:
            json.dump({"rules": {}}, f, indent=2)
        print("  • Reglas reseteadas a limpio.")

    # 3. Eliminar archivos de simulación de cuarentena
    deleted_count = 0
    if os.path.exists(QUARANTINE_DIR):
        for fname in os.listdir(QUARANTINE_DIR):
            if fname.startswith("sim_"):
                try:
                    os.remove(os.path.join(QUARANTINE_DIR, fname))
                    deleted_count += 1
                except Exception:
                    pass
    print(f"  • {deleted_count} archivos de muestra eliminados de cuarentena.")

    print("\n  ✅ ¡SISTEMA RESTAURADO Y 100% LIMPIO!")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--restore":
        restaurar_datos_reales()
    else:
        generar_simulacion()
