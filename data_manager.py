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


MAX_HISTORY_ENTRIES = 500
_PENDING_STATUSES = ("DISCARDED", "AMBIGUOUS_DATE", "REVIEW_MISSING_DATA")


def _trim_history(history: list[dict]) -> list[dict]:
    """Respeta el tope quitando primero lo ignorado y luego lo más antiguo ya resuelto.
    Lo pendiente de revisar no se quita nunca."""
    excess = len(history) - MAX_HISTORY_ENTRIES
    for removable in (
        lambda e: e.get("status") == "IGNORED_KNOWN_FORMAT",
        lambda e: e.get("status") not in _PENDING_STATUSES,
    ):
        if excess <= 0:
            break
        kept = []
        for entry in reversed(history):  # De más antigua a más reciente
            if excess > 0 and removable(entry):
                excess -= 1
            else:
                kept.append(entry)
        history = kept[::-1]
    return history


def add_history_entry(
    status: str,  # "SUCCESS", "DISCARDED", "AMBIGUOUS_DATE", "REVIEW_MISSING_DATA", "IGNORED_KNOWN_FORMAT", "SKIPPED_DUPLICATE", "NO_DOCUMENT"
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
    if original_filepath and os.path.exists(original_filepath) and status in ("DISCARDED", "AMBIGUOUS_DATE", "REVIEW_MISSING_DATA"):
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
    _save_history_raw(_trim_history(history))
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


def discard_history_entry(entry_id: str, reason: str = "Descartado manualmente por el usuario") -> bool:
    """Cambia el estado de una entrada (por ejemplo dudosa) a DISCARDED."""
    return update_history_entry(entry_id, {
        "status": "DISCARDED",
        "reason": reason,
    })


def confirm_discard_and_archive(entry_id: str, delete_file: bool = False) -> bool:
    """Confirma el descarte correcto y retira el documento de la bandeja de revisión activa."""
    entry = get_history_entry(entry_id)
    if not entry:
        return False
    if delete_file and entry.get("quarantine_file"):
        path = os.path.join(QUARANTINE_DIR, entry["quarantine_file"])
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
    return update_history_entry(entry_id, {
        "status": "DISCARDED_CONFIRMED",
        "reason": f"{entry.get('reason', '')} (Descarte confirmado por el usuario)",
    })


def delete_history_entry(entry_id: str) -> bool:
    """Elimina permanentemente una entrada del historial y su archivo de cuarentena."""
    history = _load_history_raw()
    entry = next((e for e in history if e.get("id") == entry_id), None)
    if not entry:
        return False
    if entry.get("quarantine_file"):
        path = os.path.join(QUARANTINE_DIR, entry["quarantine_file"])
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
    new_history = [e for e in history if e.get("id") != entry_id]
    _save_history_raw(new_history)
    return True


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
    date_format: str | None = None,            # "DD/MM/YYYY" o "MM/DD/YYYY"
    always_accept: bool | None = None,          # Forzar aceptación
    trusted_senders: list[str] | None = None,
    tax_id: str | None = None,
    notes: str | None = None,
    learned_from: str | None = None,            # "resolucion_ambiguedad" o "rescate_manual"
    origin_document: str | None = None,         # ej: "factura_123.pdf"
    user_reason: str | None = None,             # Motivo explícito escrito por el usuario
) -> dict:
    """Guarda o actualiza una regla aprendida para un proveedor con explicación detallada."""
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
        "learned_from": "configuracion",
        "origin_document": "",
        "user_reason": "",
        "human_explanation": "",
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
    if learned_from:
        rule["learned_from"] = learned_from
    if origin_document:
        rule["origin_document"] = origin_document
    if user_reason is not None:
        rule["user_reason"] = user_reason

    # Construir explicación humana detallada del porqué y qué hace el sistema
    reasons = []
    if rule.get("learned_from") == "resolucion_ambiguedad":
        reasons.append(f"Aprendida al resolver una fecha ambigua en el documento '{rule.get('origin_document') or 'factura'}'. Confirmaste que el proveedor emite en formato {rule.get('date_format', 'DD/MM/YYYY')}.")
    elif rule.get("learned_from") == "rescate_manual":
        doc_str = f" en '{rule.get('origin_document')}'" if rule.get("origin_document") else ""
        reason_str = f" Motivo indicado: \"{rule.get('user_reason')}\"." if rule.get("user_reason") else ""
        reasons.append(f"Aprendida tras rescatar manualmente una factura descartada{doc_str}.{reason_str} A partir de ahora el sistema forzará su aceptación.")
    elif rule.get("notes"):
        reasons.append(rule["notes"])

    rule["human_explanation"] = " ".join(reasons)
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


# ── Proveedores conocidos ────────────────────────────────────────────────────

_KNOWN_STATUSES = ("SUCCESS", "AMBIGUOUS_DATE", "SKIPPED_DUPLICATE")
_MIN_KNOWN_NAME = 4


def _email_address(sender: str | None) -> str:
    """Extrae la dirección de 'Nombre <a@b.com>' en minúsculas; '' si no hay."""
    match = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", sender or "")
    return match.group(0).lower() if match else ""


def _name_contains(known_name: str, document_name: str) -> bool:
    """True si el nombre conocido aparece como palabras completas en el del documento."""
    known = _normalize_key(known_name)
    if len(known) < _MIN_KNOWN_NAME or known == "DESCONOCIDO":
        return False
    return re.search(rf"(?<!\w){re.escape(known)}(?!\w)", _normalize_key(document_name)) is not None


def is_known_supplier(sender: str | None, supplier: str | None) -> bool:
    """¿Hay ya facturas aceptadas de este remitente o proveedor, o una regla aprendida?"""
    address = _email_address(sender)
    for entry in _load_history_raw():
        if entry.get("status") not in _KNOWN_STATUSES:
            continue
        if address and _email_address(entry.get("sender")) == address:
            return True
        if supplier and _name_contains(entry.get("supplier", ""), supplier):
            return True
    if supplier:
        return any(_name_contains(key, supplier) for key in load_vendor_rules().get("rules", {}))
    return False


# ── Formatos de documento que el usuario ha dicho que no son factura ─────────

# Documentos que seguro no son factura, sin necesidad de que nadie lo enseñe.
# (texto del dominio del remitente, formato del nombre de archivo)
BUILTIN_REJECTED_FORMATS = (("carrefour", "confirmaciondelpedido_#.pdf"),)


def _rejected_formats_file() -> str:
    return os.path.join(DATA_DIR, "rejected_formats.json")


def document_format_key(filename: str) -> str:
    """Molde del nombre: 'confirmacionDelPedido_13.pdf' -> 'confirmaciondelpedido_#.pdf'."""
    return re.sub(r"\d+", "#", (filename or "").strip().lower())


def _learnable_format(filename: str) -> str:
    """Un nombre sin ninguna palabra (13037232.pdf) no identifica un tipo de documento:
    se memoriza solo ese archivo, para no ocultar facturas con otro número."""
    key = document_format_key(filename)
    stem = key.rsplit(".", 1)[0]
    return key if any(c.isalpha() for c in stem) else (filename or "").strip().lower()


def list_rejected_formats() -> list[dict]:
    path = _rejected_formats_file()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_rejected_formats(formats: list[dict]) -> None:
    _ensure_dirs()
    with open(_rejected_formats_file(), "w", encoding="utf-8") as f:
        json.dump(formats, f, indent=2, ensure_ascii=False)


def learn_rejected_format(sender: str, filename: str, supplier: str = "") -> dict:
    """Memoriza que este formato de documento de este remitente no es una factura."""
    address = _email_address(sender)
    format_key = _learnable_format(filename)
    formats = list_rejected_formats()
    for item in formats:
        if item.get("sender") == address and item.get("format") == format_key:
            return item
    item = {
        "id": uuid.uuid4().hex[:8],
        "sender": address,
        "format": format_key,
        "supplier": supplier or "",
        "origin_document": filename,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    formats.append(item)
    _save_rejected_formats(formats)
    return item


def is_rejected_format(sender: str, filename: str) -> bool:
    """¿Este documento tiene un formato que ya se sabe que no es factura?"""
    address = _email_address(sender)
    if not address:
        return False
    format_key = document_format_key(filename)
    domain = address.rsplit("@", 1)[-1]
    if any(text in domain and fmt == format_key for text, fmt in BUILTIN_REJECTED_FORMATS):
        return True
    exact_name = (filename or "").strip().lower()
    return any(
        i.get("sender") == address and i.get("format") in (format_key, exact_name)
        for i in list_rejected_formats()
    )


def forget_rejected_format(format_id: str) -> bool:
    formats = list_rejected_formats()
    remaining = [i for i in formats if i.get("id") != format_id]
    if len(remaining) == len(formats):
        return False
    _save_rejected_formats(remaining)
    return True


# ── Correos ya revisados (para no saltarse los abiertos por error) ───────────

def _processed_emails_file() -> str:
    return os.path.join(DATA_DIR, "processed_emails.json")


def load_processed_email_ids() -> set[str] | None:
    """Claves de correos ya revisados, o None si aún no se ha hecho la primera revisión."""
    path = _processed_emails_file()
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_processed_email_ids(ids: set[str] | list[str]) -> None:
    _ensure_dirs()
    with open(_processed_emails_file(), "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f, indent=0, ensure_ascii=False)


def remember_processed_email(key: str) -> None:
    """Anota un correo revisado. Sin primera revisión hecha no escribe nada: si
    creara el archivo, los correos ya abiertos parecerían nuevos y se reprocesarían."""
    ids = load_processed_email_ids()
    if ids is not None and key not in ids:
        ids.add(key)
        save_processed_email_ids(ids)


# ── Estadísticas de Resumen ──────────────────────────────────────────────────

def get_stats() -> dict:
    """Calcula totales para las tarjetas informativas del dashboard."""
    history = [e for e in _load_history_raw() if e.get("status") != "IGNORED_KNOWN_FORMAT"]
    success = sum(1 for e in history if e.get("status") == "SUCCESS")
    discarded = sum(1 for e in history if e.get("status") == "DISCARDED")
    ambiguous = sum(1 for e in history if e.get("status") in ("AMBIGUOUS_DATE", "REVIEW_MISSING_DATA"))
    rules_count = len(load_vendor_rules().get("rules", {}))

    return {
        "total_processed": len(history),
        "success_count": success,
        "discarded_count": discarded,
        "ambiguous_count": ambiguous,
        "rules_count": rules_count,
        "requires_review": discarded + ambiguous,
    }
