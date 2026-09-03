"""
data_manager.py — Capa de persistencia local para historial, cuarentena y reglas aprendidas.

Gestiona:
1. data/invoices_history.json  - Registro de facturas procesadas, descartadas y dudosas.
2. data/vendor_rules.json      - Motor de aprendizaje continuo por proveedor.
3. data/schedule_config.json   - Configuración del programador desatendido.
4. data/quarantine/            - Almacén de archivos PDF/imágenes para previsualización.
"""

import os
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
import unicodedata
import re

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
QUARANTINE_DIR = os.path.join(DATA_DIR, "quarantine")
HISTORY_FILE = os.path.join(DATA_DIR, "invoices_history.json")
RULES_FILE = os.path.join(DATA_DIR, "vendor_rules.json")
SCHEDULE_FILE = os.path.join(DATA_DIR, "schedule_config.json")


def _ensure_dirs() -> None:
    """Asegura que los directorios de datos y cuarentena existan."""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(QUARANTINE_DIR, exist_ok=True)


# ── Historial de Facturas ───────────────────────────────────────────────────

def _load_history_raw() -> list[dict]:
    _ensure_dirs()
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_history_raw(entries: list[dict]) -> None:
    _ensure_dirs()
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


def add_history_entry(
    status: str,  # "SUCCESS", "DISCARDED", "AMBIGUOUS_DATE"
    filename: str,
    supplier: str,
    date_str: str,
    drive_file_id: str | None = None,
    drive_folder_id: str | None = None,
    drive_folder_path: str | None = None,
    reason: str | None = None,
    original_filepath: str | None = None,
    email_subject: str = "",
    sender: str = "",
    detected_dates: list[str] | None = None,
    amount: str | None = None,
) -> str:
    """Registra una factura en el historial y guarda copia en cuarentena si requiere revisión."""
    _ensure_dirs()
    entry_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    quarantine_filename = None

    # Si fue descartada o es dudosa, copiamos el archivo a cuarentena para previsualizarlo
    if original_filepath and os.path.exists(original_filepath) and status in ("DISCARDED", "AMBIGUOUS_DATE"):
        ext = Path(original_filepath).suffix.lower()
        target_name = f"{entry_id}{ext}"
        target_path = os.path.join(QUARANTINE_DIR, target_name)
        try:
            shutil.copy2(original_filepath, target_path)
            quarantine_filename = target_name
        except Exception as e:
            print(f"[WARN] No se pudo copiar a cuarentena: {e}")

    entry = {
        "id": entry_id,
        "status": status,
        "filename": filename,
        "supplier": supplier.strip().upper(),
        "date_str": date_str,  # ej: "06-26" o "2026-06-09"
        "drive_file_id": drive_file_id,
        "drive_folder_id": drive_folder_id,
        "drive_folder_path": drive_folder_path,
        "reason": reason or "",
        "quarantine_file": quarantine_filename,
        "email_subject": email_subject,
        "sender": sender,
        "detected_dates": detected_dates or [],
        "amount": amount,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }

    history = _load_history_raw()
    history.insert(0, entry)  # Más reciente primero
    # Mantener máximo 500 registros en disco
    if len(history) > 500:
        history = history[:500]
    _save_history_raw(history)
    return entry_id


def get_history(status: str | None = None, limit: int = 100) -> list[dict]:
    """Devuelve las entradas del historial filtradas opcionalmente por estado."""
    history = _load_history_raw()
    if status:
        history = [e for e in history if e.get("status") == status]
    return history[:limit]


def get_history_entry(entry_id: str) -> dict | None:
    """Busca una entrada concreta del historial."""
    history = _load_history_raw()
    for e in history:
        if e.get("id") == entry_id:
            return e
    return None


def update_history_entry(entry_id: str, updates: dict) -> bool:
    """Actualiza campos de una entrada del historial."""
    history = _load_history_raw()
    updated = False
    for i, e in enumerate(history):
        if e.get("id") == entry_id:
            history[i].update(updates)
            history[i]["updated_at"] = datetime.now().isoformat(timespec="seconds")
            updated = True
            break
    if updated:
        _save_history_raw(history)
    return updated


def get_quarantine_path(entry_id: str) -> str | None:
    """Devuelve la ruta absoluta del archivo en cuarentena si existe."""
    entry = get_history_entry(entry_id)
    if not entry or not entry.get("quarantine_file"):
        return None
    path = os.path.join(QUARANTINE_DIR, entry["quarantine_file"])
    return path if os.path.exists(path) else None


# ── Motor de Aprendizaje Continuo (Reglas por Proveedor) ─────────────────────

def _normalize_key(name: str) -> str:
    """Normaliza el nombre para que coincida sin tildes ni caracteres raros."""
    name = (name or "").upper().strip()
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    name = re.sub(r"[.,\-_:;()/]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def load_vendor_rules() -> dict:
    """Carga todas las reglas de aprendizaje almacenadas."""
    _ensure_dirs()
    if not os.path.exists(RULES_FILE):
        return {"rules": {}}
    try:
        with open(RULES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) and "rules" in data else {"rules": {}}
    except Exception:
        return {"rules": {}}


def save_vendor_rule(
    supplier_name: str,
    date_format: str | None = None,       # "DD/MM/YYYY" o "MM/DD/YYYY"
    always_accept: bool | None = None,     # Forzar aceptación
    trusted_senders: list[str] | None = None,
    tax_id: str | None = None,
    notes: str | None = None,
) -> dict:
    """Guarda o actualiza una regla aprendida para un proveedor."""
    data = load_vendor_rules()
    key = _normalize_key(supplier_name)
    if not key:
        return {}

    rule = data["rules"].get(key, {
        "display_name": supplier_name.strip().upper(),
        "date_format": "DD/MM/YYYY",
        "always_accept": False,
        "trusted_senders": [],
        "tax_id": "",
        "notes": "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })

    if date_format:
        rule["date_format"] = date_format.upper()
    if always_accept is not None:
        rule["always_accept"] = bool(always_accept)
    if trusted_senders is not None:
        rule["trusted_senders"] = list(set(rule.get("trusted_senders", []) + trusted_senders))
    if tax_id:
        rule["tax_id"] = tax_id.strip().upper()
    if notes is not None:
        rule["notes"] = notes

    rule["updated_at"] = datetime.now().isoformat(timespec="seconds")
    data["rules"][key] = rule

    _ensure_dirs()
    with open(RULES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return rule


def get_vendor_rule(supplier_name: str) -> dict | None:
    """Consulta la regla aprendida para un proveedor mediante coincidencia normalizada."""
    data = load_vendor_rules()
    key = _normalize_key(supplier_name)
    rules = data.get("rules", {})

    # 1. Match exacto normalizado
    if key in rules:
        return rules[key]

    # 2. Match parcial si el proveedor contiene la clave o viceversa
    for r_key, r_val in rules.items():
        if r_key and (r_key in key or key in r_key):
            return r_val
    return None


def delete_vendor_rule(supplier_name: str) -> bool:
    """Elimina una regla guardada."""
    data = load_vendor_rules()
    key = _normalize_key(supplier_name)
    if key in data.get("rules", {}):
        del data["rules"][key]
        with open(RULES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    return False


# ── Configuración del Programador (Scheduler) ───────────────────────────────

DEFAULT_SCHEDULE = {
    "enabled": False,
    "frequency": "weekly",  # "weekly", "monthly", "daily"
    "day_of_week": 0,       # 0 = Lunes, 6 = Domingo
    "day_of_month": 1,      # 1 a 31
    "hour": 9,              # 0 a 23
    "minute": 0,            # 0 a 59
    "last_run": None,
}


def load_schedule_config() -> dict:
    """Carga la configuración del programador periódico."""
    _ensure_dirs()
    if not os.path.exists(SCHEDULE_FILE):
        return DEFAULT_SCHEDULE.copy()
    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            config = DEFAULT_SCHEDULE.copy()
            config.update(data)
            return config
    except Exception:
        return DEFAULT_SCHEDULE.copy()


def save_schedule_config(config: dict) -> dict:
    """Guarda la configuración del programador periódico."""
    _ensure_dirs()
    current = load_schedule_config()
    current.update(config)
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)
    return current


# ── Estadísticas de Resumen ──────────────────────────────────────────────────

def get_stats() -> dict:
    """Calcula totales para las tarjetas informativas del dashboard."""
    history = _load_history_raw()
    success = sum(1 for e in history if e.get("status") == "SUCCESS")
    discarded = sum(1 for e in history if e.get("status") == "DISCARDED")
    ambiguous = sum(1 for e in history if e.get("status") == "AMBIGUOUS_DATE")
    rules_count = len(load_vendor_rules().get("rules", {}))

    return {
        "total_processed": len(history),
        "success_count": success,
        "discarded_count": discarded,
        "ambiguous_count": ambiguous,
        "rules_count": rules_count,
        "requires_review": discarded + ambiguous,
    }
