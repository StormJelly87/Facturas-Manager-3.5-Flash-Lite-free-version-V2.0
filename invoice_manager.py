#!/usr/bin/env python3
"""
Invoice Manager — Gestor autónomo de facturas.

Busca en Gmail correos no leídos con facturas adjuntas, valida cada adjunto
con Gemini (anti-falsos positivos) y sube los archivos confirmados a
Google Drive organizados en carpetas MM-YY / PROVEEDOR.
"""

import imaplib
import email
import os
import sys
import json
import tempfile
import time
import re
import unicodedata
import hashlib
from email.header import decode_header
from pathlib import Path
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from dateutil import parser as dateutil_parser
import pdfplumber

from enviomedical_portal import (
    EnvioMedicalPortal,
    EnvioMedicalError,
    load_state,
    save_state,
    compute_desde_date,
    DELAY_BETWEEN_DOWNLOADS,
)

from dotenv import load_dotenv
import google.generativeai as genai
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ── Configuración ────────────────────────────────────────────────────────────

load_dotenv()

EMAIL_ADDRESS    = os.getenv("EMAIL_ADDRESS")
APP_PASSWORD     = os.getenv("APP_PASSWORD")
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY")
DRIVE_ROOT_ID    = os.getenv("DRIVE_ROOT_FOLDER_ID")

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT   = 993

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "models/gemini-3.5-flash-lite")

# Scopes necesarios para Google Drive (acceso completo para ver carpetas compartidas)
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

# ── Proveedores ignorados por correo (sus facturas llegan por otro canal) ────
# Los correos de estos dominios se marcan como leidos SIN procesar sus adjuntos.
# EnvioMedical: las facturas validas se descargan del portal B2B (ver enviomedical_portal.py)
IGNORED_SENDER_DOMAINS = {
    d.strip().lower()
    for d in os.getenv("IGNORE_SENDER_DOMAINS", "enviomedical.com").split(",")
    if d.strip()
}

# ── Portal B2B EnvioMedical (Titania Tools) ──────────────────────────────────
ENVIO_USER          = os.getenv("ENVIO_USER")
ENVIO_PASS          = os.getenv("ENVIO_PASS")
ENVIO_SUPPLIER_NAME = os.getenv("ENVIO_SUPPLIER_NAME", "ENVÍOMÉDICAL")
ENVIO_LOOKBACK_DAYS = int(os.getenv("ENVIO_LOOKBACK_DAYS", "45"))

# Extensiones que consideramos «factura adjunta»
INVOICE_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".webp"}

# Por debajo de este tamaño, una imagen integrada en el correo se considera logo o firma
INLINE_IMAGE_MIN_BYTES = 50 * 1024
MAX_ATTACHMENT_NAME = 120
WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL",
                          *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

# Días hacia atrás en los que se revisan también los correos ya abiertos
READ_EMAIL_LOOKBACK_DAYS = int(os.getenv("READ_EMAIL_LOOKBACK_DAYS", "60"))
HEADER_FETCH_BATCH = 200

# Rate limiting: segundos de espera entre llamadas a Gemini
RATE_LIMIT_DELAY  = 4
MAX_RETRIES       = 3

# Carpeta de facturas de referencia (para rescate por similitud)
REFERENCE_INVOICES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Facturas ejemplo")

# Palabras clave fiscales para fingerprinting de facturas de referencia
_FISCAL_KEYWORDS = {
    "factura", "invoice", "nif", "cif", "vat", "iva",
    "base imponible", "total", "importe", "subtotal",
    "tipo iva", "neto", "bruto", "descuento",
    "fecha factura", "fecha emision", "tax", "fiscal",
    "razon social", "domicilio", "direccion",
    "numero factura", "n factura", "fra",
}

# ── Alias y Mapeos de Proveedores (Razón Social -> Nombre Comercial) ─────────
# Se cargan dinámicamente desde supplier_aliases.json si existe (ver supplier_aliases.example.json)
ALIASES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supplier_aliases.json")

def _load_supplier_mappings() -> tuple[dict, dict]:
    """Carga los mapeos de proveedores desde supplier_aliases.json si existe."""
    if os.path.exists(ALIASES_FILE):
        try:
            with open(ALIASES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("aliases", {}), data.get("tax_id_map", {})
        except Exception as e:
            log(f"Aviso al leer {ALIASES_FILE}: {e}", "WARN")
    return {}, {}

SUPPLIER_ALIASES, TAX_ID_SUPPLIER_MAP = _load_supplier_mappings()


def _normalize_tax_id(tax_id: str | None) -> str:
    """Quita separadores y el prefijo de país español: 'ES-A28425270' -> 'A28425270'."""
    cleaned = re.sub(r"[\s.\-]", "", tax_id or "").upper()
    return cleaned[2:] if cleaned.startswith("ES") else cleaned


def _load_own_tax_ids() -> set[str]:
    """CIF de la propia empresa (local, en supplier_aliases.json): aparece como cliente
    en las facturas y la IA puede confundirlo con el del emisor."""
    if os.path.exists(ALIASES_FILE):
        try:
            with open(ALIASES_FILE, "r", encoding="utf-8") as f:
                return {_normalize_tax_id(t) for t in json.load(f).get("own_tax_ids", [])}
        except Exception as e:
            log(f"Aviso al leer {ALIASES_FILE}: {e}", "WARN")
    return set()


OWN_TAX_IDS = _load_own_tax_ids()

# CIF de sociedades, DNI y NIE. Quedan fuera N y W, que son entidades extranjeras.
_SPANISH_TAX_ID = re.compile(r"^(?:[ABCDEFGHJPQRSUV]\d{7}[0-9A-J]|\d{8}[A-Z]|[XYZ]\d{7}[A-Z])$")
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4}|\d{2})\b")


def _spanish_control_is_valid(tax_id: str) -> bool:
    """Comprueba el dígito o letra de control: distingue un identificador español de
    otro extranjero con la misma forma, como un RUT chileno 12345678K."""
    if tax_id[0] in "XYZ" or tax_id[0].isdigit():
        number = "XYZ".find(tax_id[0]) if tax_id[0] in "XYZ" else ""
        return "TRWAGMYFPDXBNJZSQVHLCKE"[int(f"{number}{tax_id[1 if number != '' else 0:8]}") % 23] == tax_id[8]
    digits = [int(c) for c in tax_id[1:8]]
    total = sum(digits[1::2]) + sum(sum(divmod(d * 2, 10)) for d in digits[0::2])
    control = (10 - total % 10) % 10
    return tax_id[8] in (str(control), "JABCDEFGHI"[control])


def _is_spanish_tax_id(tax_id: str | None) -> bool:
    """¿Es el identificador de un emisor español, y no el de la propia empresa?"""
    cleaned = _normalize_tax_id(tax_id)
    return (
        bool(_SPANISH_TAX_ID.match(cleaned))
        and _spanish_control_is_valid(cleaned)
        and cleaned not in {_normalize_tax_id(t) for t in OWN_TAX_IDS}
    )


_MONTH_NAMES = {
    # Español completo
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
    # Español abreviado
    "ene": 1, "feb": 2, "mar": 3, "abr": 4,
    "jun": 6, "jul": 7, "ago": 8,
    "sep": 9, "oct": 10, "nov": 11, "dic": 12,
    # Inglés completo
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    # Inglés abreviado
    "jan": 1, "apr": 4, "aug": 8, "dec": 12,
    # Francés
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
    # Italiano
    "gennaio": 1, "febbraio": 2, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7,
    "settembre": 9, "ottobre": 10, "dicembre": 12,
    # Portugués
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "maio": 5, "junho": 6, "julho": 7,
    "setembro": 9, "outubro": 10, "dezembro": 12,
    # Alemán
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "juni": 6, "juli": 7, "oktober": 10, "dezember": 12,
}
_MONTH_PATTERN = "|".join(sorted(_MONTH_NAMES, key=len, reverse=True))
_WRITTEN_DATE = re.compile(
    rf"\b(\d{{1,2}})\.?[\s\-/]+(?:de\s+)?({_MONTH_PATTERN})\b(?:[\s\-/]+(?:de\s+)?(\d{{4}})\b)?"
)


_AMERICAN_WRITTEN_DATE = re.compile(
    rf"\b({_MONTH_PATTERN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b"
)
_MONTH_AND_YEAR = re.compile(rf"\b({_MONTH_PATTERN})\.?\s+(?:de\s+)?(\d{{4}})\b")

_DATE_TOKEN = (
    r"\d{1,2}[/.\-]\d{1,2}[/.\-](?:\d{4}|\d{2})"
    rf"|\d{{1,2}}\.?[\s\-/]+(?:de\s+)?(?:{_MONTH_PATTERN})\b(?:[\s\-/]+(?:de\s+)?\d{{4}})?"
    rf"|(?:{_MONTH_PATTERN})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
)


def _label_regex(labels: str) -> re.Pattern:
    """Etiqueta seguida de una fecha en la misma línea, sin otras palabras por medio.
    En una tabla la fecha de la línea siguiente puede ser de otra columna: esas no cuentan."""
    return re.compile(rf"\b(?:{labels})\b[ \t:.\-]*(?:de[ \t]+)?({_DATE_TOKEN})")


_INVOICE_DATE_LABELS = _label_regex(
    r"fecha\s+(?:de\s+)?(?:la\s+)?(?:factura|emisi[oó]n|expedici[oó]n)"
    r"|invoice\s+date|issue\s+date|date\s+of\s+issue|date\s+(?:de\s+(?:la\s+)?)?facture|date\s+d.[eé]mission"
    r"|rechnungsdatum|data\s+(?:d[ea]\s+)?(?:fattura|factura|fatura|emiss[aã]o|emissione)"
)
_BARE_DATE_LABELS = _label_regex(r"fecha|date|data|datum")
_OTHER_DATE_LABELS = _label_regex(
    r"vencimiento|fecha\s+de\s+pago|due\s+date|payment\s+due|due|f[aä]llig(?:keit)?"
    r"|lieferdatum|delivery\s+date|entrega|pedido|order\s+date|albar[aá]n"
    r"|per[ií]odo(?:\s+de\s+\w+)?|fecha\s+de\s+(?:cobro|servicio)|[eé]ch[eé]ance|scadenza|vencimento"
)
_WORD_BEFORE_OTHER_DATE = re.compile(
    r"(?:due|delivery|order|payment|shipping|expiry|vencimiento|pago|entrega|pedido|albar[aá]n)[\s:]*$"
)


def _labelled_dates(text: str, labels: re.Pattern) -> list[dict]:
    """Fechas válidas que siguen a una etiqueta, en el orden del documento."""
    found = []
    for match in labels.finditer(text or ""):
        if _WORD_BEFORE_OTHER_DATE.search(text[max(0, match.start() - 20):match.start()]):
            continue  # "due date: ..." no es una fecha de factura
        for candidate in _find_text_dates(match.group(1))[:1]:
            try:
                datetime(candidate["year"] or 2001, candidate["month"], candidate["day"])
                found.append(candidate)
            except ValueError:
                pass
    return found


def _email_datetime(raw_date: str | None) -> datetime | None:
    """Fecha de la cabecera del correo, o None si falta o no se entiende."""
    try:
        return parsedate_to_datetime(raw_date).replace(tzinfo=None)
    except (TypeError, ValueError, IndexError, AttributeError):
        return None


def _find_text_dates(text: str) -> list[dict]:
    """Todas las fechas del documento leídas como día/mes, numéricas y con el mes en letras."""
    dates = []
    for match in _NUMERIC_DATE.finditer(text or ""):
        day, month, year = match.groups()
        dates.append({
            "day": int(day), "month": int(month),
            "year": int(year) if len(year) == 4 else 2000 + int(year),
            "written": False,
            "strict": "." not in match.group(0) and len(year) == 4,
        })
    for match in _WRITTEN_DATE.finditer(text or ""):
        day, month, year = match.groups()
        dates.append({
            "day": int(day), "month": _MONTH_NAMES[month],
            "year": int(year) if year else None,
            "written": True, "strict": False,
        })
    for match in _AMERICAN_WRITTEN_DATE.finditer(text or ""):
        month, day, year = match.groups()
        dates.append({
            "day": int(day), "month": _MONTH_NAMES[month],
            "year": int(year), "written": True, "strict": False,
        })
    return dates


def _same_date(found: dict, dt: datetime) -> bool:
    return found["day"] == dt.day and found["month"] == dt.month and found["year"] in (None, dt.year)


def _text_confirms_day_first(text: str, dt: datetime) -> bool:
    """¿El texto del documento dice esa fecha como día/mes, sin señales de mes/día?"""
    dates = _NUMERIC_DATE.findall(text or "")
    # Una fecha como 10/30/2026 delata un documento que escribe primero el mes
    if any(int(first) <= 12 < int(second) for first, second, _ in dates):
        return False
    return any(
        int(first) == dt.day and int(second) == dt.month and str(dt.year).endswith(year)
        for first, second, year in dates
    )

# ── Utilidades ───────────────────────────────────────────────────────────────

LOG_CALLBACK = None

def set_log_callback(cb) -> None:
    """Registra una función callback para recibir mensajes de log en tiempo real."""
    global LOG_CALLBACK
    LOG_CALLBACK = cb


def log(msg: str, level: str = "INFO") -> None:
    """Imprime un mensaje con timestamp (seguro para consolas Windows)."""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        # Fallback: eliminar caracteres no soportados por la consola
        print(line.encode("ascii", errors="replace").decode("ascii"))

    # Reenviar al panel en vivo (salvo lineas separadoras). Un fallo del panel
    # nunca debe interrumpir el procesamiento de facturas.
    if LOG_CALLBACK and msg.strip().strip("="):
        try:
            LOG_CALLBACK(msg.strip(), level)
        except Exception:
            pass


def decode_mime_header(raw: str | None) -> str:
    """Decodifica una cabecera MIME (Subject, From, etc.)."""
    if raw is None:
        return ""
    parts = decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


def sanitize_folder_name(name: str) -> str:
    """Limpia un nombre para usarlo como carpeta en Drive."""
    name = name.strip().upper()
    # Eliminar caracteres problemáticos
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    # Colapsar espacios múltiples
    name = re.sub(r'\s+', ' ', name)
    return name if name else "DESCONOCIDO"


# ── Gmail (IMAP) ─────────────────────────────────────────────────────────────

def connect_gmail() -> imaplib.IMAP4_SSL:
    """Conecta al servidor IMAP de Gmail con App Password."""
    log(f"Conectando a Gmail como {EMAIL_ADDRESS}...")
    mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
    mail.login(EMAIL_ADDRESS, APP_PASSWORD)
    log("Conexion establecida.")
    return mail


def email_key(message_id: str | None, uid) -> str:
    """Identificador estable de un correo: su Message-ID sin corchetes, o 'uid:<n>'."""
    cleaned = (message_id or "").strip().strip("<>").strip()
    return cleaned or f"uid:{uid}"


def _fetch_email_keys(mail: imaplib.IMAP4_SSL, uids: list[str]) -> dict[str, str]:
    """Devuelve {uid: clave} pidiendo solo la cabecera Message-ID, sin marcar nada como leído."""
    keys = {}
    for start in range(0, len(uids), HEADER_FETCH_BATCH):
        chunk = uids[start:start + HEADER_FETCH_BATCH]
        status, data = mail.uid("FETCH", ",".join(chunk), "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        if status != "OK":
            raise RuntimeError("No se pudieron leer las cabeceras de los correos abiertos")
        for item in data or []:
            if not isinstance(item, tuple):
                continue
            match = re.search(rb"\bUID (\d+)", item[0])
            if match:
                uid = match.group(1).decode()
                keys[uid] = email_key(email.message_from_bytes(item[1]).get("Message-ID"), uid)
    # Un correo sin cabecera en la respuesta sigue teniendo clave propia
    for uid in uids:
        keys.setdefault(uid, email_key(None, uid))
    return keys


def _find_opened_emails(mail: imaplib.IMAP4_SSL, unread_uids: list[str]) -> list[str]:
    """
    Correos recientes con factura que están abiertos y que el programa no ha revisado.

    La primera vez no devuelve ninguno: todo lo ya abierto se da por revisado, para
    no volver a analizar el buzón entero. Si algo falla devuelve lista vacía y los
    no leídos se procesan igualmente.
    """
    import data_manager

    query = (
        f'X-GM-RAW "is:read newer_than:{READ_EMAIL_LOOKBACK_DAYS}d has:attachment '
        '(factura OR facturas OR invoice)"'
    )
    try:
        status, data = mail.uid("SEARCH", None, query)
        if status != "OK":
            raise RuntimeError("Gmail no aceptó la búsqueda de correos abiertos")
        unread = set(unread_uids)
        uids = [u.decode() for u in data[0].split() if u.decode() not in unread]
        keys = _fetch_email_keys(mail, uids)
        processed = data_manager.load_processed_email_ids()
        if processed is None:
            data_manager.save_processed_email_ids(set(keys.values()))
            log(f"Primera revisión de correos abiertos: {len(keys)} correo(s) de los últimos {READ_EMAIL_LOOKBACK_DAYS} días se dan por revisados.")
            return []
        pending = [uid for uid in uids if keys[uid] not in processed]
        if pending:
            log(f"{len(pending)} correo(s) abierto(s) sin revisar de los últimos {READ_EMAIL_LOOKBACK_DAYS} días.")
        return pending
    except Exception as e:
        log(f"No se pudieron revisar los correos ya abiertos: {e}. Se procesan solo los no leídos.", "WARN")
        return []


def search_invoice_emails(mail: imaplib.IMAP4_SSL) -> list[str]:
    """
    Busca correos con 'factura', 'facturas' o 'invoice': los no leídos y los
    abiertos recientes que el programa aún no ha revisado. Devuelve una lista de UIDs.
    """
    mail.select("INBOX")

    # Gmail IMAP soporta X-GM-RAW para búsquedas avanzadas
    query = (
        'X-GM-RAW "is:unread has:attachment '
        '(factura OR facturas OR invoice)"'
    )
    log(f"Buscando correos con: {query}")
    status, data = mail.uid("SEARCH", None, query)

    if status != "OK":
        log("No se pudo realizar la busqueda.", "ERROR")
        return []

    uids = [uid.decode() for uid in data[0].split()]
    log(f"{len(uids)} correo(s) encontrado(s).")
    return uids + _find_opened_emails(mail, uids)


def fetch_email(mail: imaplib.IMAP4_SSL, uid: str) -> email.message.Message:
    """Descarga un mensaje completo por UID, sin marcarlo como leído."""
    status, data = mail.uid("FETCH", uid, "(BODY.PEEK[])")
    if status != "OK":
        raise RuntimeError(f"No se pudo descargar el correo UID {uid}")
    raw = data[0][1]
    return email.message_from_bytes(raw)


def _safe_attachment_name(filename: str) -> str:
    """Deja solo el nombre del archivo: sin rutas ni caracteres no validos en Windows."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")
    # Windows no permite crear archivos con nombres de dispositivo como PRN.pdf
    if name.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    # Windows limita la longitud de las rutas: recortar conservando la extension
    if len(name) > MAX_ATTACHMENT_NAME:
        suffix = Path(name).suffix[:10]
        name = name[:MAX_ATTACHMENT_NAME - len(suffix)] + suffix
    return name


def extract_attachments(msg: email.message.Message, tmp_dir: str) -> list[str]:
    """
    Extrae los documentos válidos (PDF, imágenes) del mensaje.
    Devuelve una lista de rutas a los archivos extraídos.

    Un PDF se acepta siempre, sea cual sea su Content-Disposition: Carrefour los
    envía como 'inline' y Gmail los muestra igualmente como adjuntos.
    Una imagen 'inline' pequeña se ignora porque suele ser un logo o una firma.
    """
    saved = []
    for part in msg.walk():
        if part.is_multipart():
            continue

        disposition = part.get_content_disposition()
        is_pdf_type = part.get_content_type() == "application/pdf"

        filename = _safe_attachment_name(decode_mime_header(part.get_filename()))
        if not filename:
            if not is_pdf_type:
                continue
            filename = "documento_sin_nombre.pdf"
        elif is_pdf_type and Path(filename).suffix.lower() != ".pdf":
            filename += ".pdf"

        ext = Path(filename).suffix.lower()
        if ext not in INVOICE_EXTENSIONS:
            log(f"  Adjunto ignorado (extension {ext}): {filename}")
            continue

        payload = part.get_payload(decode=True)
        if not payload:
            log(f"  Adjunto ignorado (vacio): {filename}", "WARN")
            continue

        if ext != ".pdf" and disposition != "attachment" and len(payload) < INLINE_IMAGE_MIN_BYTES:
            log(f"  Imagen integrada ignorada (probable logo, {len(payload)} bytes): {filename}")
            continue

        filepath = os.path.join(tmp_dir, filename)
        # Evitar colisiones de nombre
        counter = 1
        base, extension = os.path.splitext(filepath)
        while os.path.exists(filepath):
            filepath = f"{base}_{counter}{extension}"
            counter += 1

        with open(filepath, "wb") as f:
            f.write(payload)

        log(f"  Adjunto guardado: {filename}")
        saved.append(filepath)

    return saved


def mark_as_read(mail: imaplib.IMAP4_SSL, uid: str) -> None:
    """Marca un correo como leído (flag \\Seen)."""
    mail.uid("STORE", uid, "+FLAGS", "(\\Seen)")
    log(f"  Correo UID {uid} marcado como leido.")


# ── Gemini (google-generativeai SDK) ─────────────────────────────────────────

def init_gemini() -> genai.GenerativeModel:
    """Inicializa el cliente Gemini."""
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(GEMINI_MODEL)
    log(f"Gemini inicializado (modelo: {GEMINI_MODEL}).")
    return model


# Mapa de extensiones a MIME types para Gemini inline_data
MIME_MAP = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}


class GeminiAPIError(Exception):
    """Error de API de Gemini (rate limit, etc.) — no debe marcar como leido."""
    pass


def _is_amazon_sender(sender: str) -> bool:
    """
    Comprueba si el remitente del correo pertenece a un dominio de Amazon.
    Devuelve True si contiene @amazon.es, @amazon.com, @amazon.de, etc.
    """
    sender_lower = sender.lower()
    # Buscar cualquier @amazon. seguido de un TLD
    return bool(re.search(r'@amazon\.\w+', sender_lower))


def _is_ignored_sender(sender: str) -> str | None:
    """
    Comprueba si el remitente pertenece a un dominio de la lista de ignorados
    (IGNORED_SENDER_DOMAINS). Devuelve el dominio que coincide o None.

    Estos proveedores se gestionan por otro canal (p. ej. descarga directa
    del portal B2B), por lo que sus correos NO deben procesarse.
    """
    if not IGNORED_SENDER_DOMAINS:
        return None
    # Extraer la direccion de correo del remitente ('Nombre <a@b.com>' -> a@b.com)
    m = re.search(r'[\w.+-]+@([\w-]+(?:\.[\w-]+)+)', sender or "")
    if not m:
        return None
    domain = m.group(1).lower()
    for ignored in IGNORED_SENDER_DOMAINS:
        if domain == ignored or domain.endswith("." + ignored):
            return ignored
    return None


# Regex para validar CIF/NIF/VAT: acepta códigos alfanuméricos de 8-15 chars,
# con opcionalmente 2 letras de país al principio.
# Ejemplos válidos: ESN0057922G, ESB12345678, B12345678, DE123456789, FR12345678901
_TAX_ID_PATTERN = re.compile(
    r'\b[A-Z]{0,2}[A-Z0-9]{7,12}\b',
    re.IGNORECASE
)


def _is_valid_tax_id(tax_id: str) -> bool:
    """
    Valida si un string parece un CIF/NIF/VAT ID legítimo.
    Acepta códigos alfanuméricos de 8-15 caracteres con prefijo de país opcional.
    Soporta formatos con guiones o puntos (ej: Y1234567-Z, B-60331451).
    """
    if not tax_id or tax_id.upper() in ("DESCONOCIDO", "N/A", "NO", "NONE", ""):
        return False

    cleaned = tax_id.strip()

    # Buscar un patrón válido de CIF/VAT en el texto
    match = _TAX_ID_PATTERN.search(cleaned)
    if match:
        if match.group() != cleaned:
            log(f"  [DEBUG] CIF potencial detectado: {match.group()} (dentro de '{cleaned}')")
        return True

    # Intentar limpiando guiones, puntos y espacios (ej. Y1234567-Z -> Y1234567Z)
    cleaned_no_sep = re.sub(r'[\s\.\-]', '', cleaned)
    match_no_sep = _TAX_ID_PATTERN.search(cleaned_no_sep)
    if match_no_sep:
        log(f"  [DEBUG] CIF detectado tras normalizar separadores: {match_no_sep.group()}")
        return True

    return False


# Regex para buscar CIF/NIF/VAT directamente en texto extraído de PDF
_PDF_TAX_ID_REGEX = re.compile(
    r'(?:IVA|VAT|CIF|NIF|NIF[\-\s]?IVA|ID\s*(?:de\s*)?IVA|VAT\s*ID|Tax\s*ID)[\s:]*([A-Z]{0,2}[A-Z0-9\-]{7,14})\b',
    re.IGNORECASE
)


def _extract_tax_id_from_pdf(filepath: str) -> str | None:
    """
    Plan B: extrae texto crudo del PDF con pdfplumber y busca
    un CIF/NIF/VAT con regex. Devuelve el código encontrado o None.
    """
    ext = Path(filepath).suffix.lower()
    if ext != ".pdf":
        return None

    try:
        full_text = ""
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    full_text += page_text + "\n"

        if not full_text.strip():
            log(f"  [FALLBACK] No se pudo extraer texto del PDF.", "WARN")
            return None

        # Buscar patrón CIF/VAT con etiqueta en el texto
        match = _PDF_TAX_ID_REGEX.search(full_text)
        if match:
            found = match.group(1)
            log(f"  [FALLBACK] CIF/VAT encontrado en texto del PDF: {found}")
            return found

        # Búsqueda más amplia: cualquier código que parezca CIF sin etiqueta
        broad_match = _TAX_ID_PATTERN.search(full_text)
        if broad_match:
            log(f"  [FALLBACK] CIF potencial en texto del PDF: {broad_match.group()}")
            return broad_match.group()

        log(f"  [FALLBACK] No se encontró CIF/VAT en el texto del PDF.", "WARN")
        return None

    except Exception as e:
        log(f"  [FALLBACK] Error al leer PDF con pdfplumber: {e}", "ERROR")
        return None


# ── Sistema de Facturas de Referencia ────────────────────────────────────────

def _extract_fiscal_fingerprint(filepath: str) -> set[str] | None:
    """
    Extrae un 'fingerprint fiscal' de un PDF: el conjunto de palabras clave
    fiscales encontradas en su texto. Devuelve None si no se puede leer.
    """
    ext = Path(filepath).suffix.lower()
    if ext != ".pdf":
        return None

    try:
        full_text = ""
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    full_text += page_text + "\n"

        if not full_text.strip():
            return None

        text_lower = full_text.lower()
        # Normalizar: quitar acentos para comparación
        text_normalized = unicodedata.normalize("NFD", text_lower)
        text_normalized = "".join(c for c in text_normalized if unicodedata.category(c) != "Mn")

        found_keywords = set()
        for keyword in _FISCAL_KEYWORDS:
            # Normalizar keyword también
            kw_normalized = unicodedata.normalize("NFD", keyword)
            kw_normalized = "".join(c for c in kw_normalized if unicodedata.category(c) != "Mn")
            if kw_normalized in text_normalized:
                found_keywords.add(keyword)

        return found_keywords if found_keywords else None

    except Exception:
        return None


def _load_reference_fingerprints() -> list[dict]:
    """
    Lee todos los PDFs de la carpeta 'Facturas ejemplo/' y extrae sus
    fingerprints fiscales. Se ejecuta una vez al inicio y se cachea.

    Returns:
        Lista de dicts con {filename, keywords} por cada referencia.
    """
    if not os.path.isdir(REFERENCE_INVOICES_DIR):
        return []

    fingerprints = []
    for fname in os.listdir(REFERENCE_INVOICES_DIR):
        fpath = os.path.join(REFERENCE_INVOICES_DIR, fname)
        if not os.path.isfile(fpath):
            continue
        if Path(fname).suffix.lower() != ".pdf":
            continue

        fp = _extract_fiscal_fingerprint(fpath)
        if fp:
            fingerprints.append({"filename": fname, "keywords": fp})
            log(f"  [REF] Fingerprint cargado: '{fname}' ({len(fp)} keywords: {', '.join(sorted(fp))})")

    return fingerprints


def _matches_reference_invoice(filepath: str, reference_fps: list[dict], threshold: float = 0.60) -> bool:
    """
    Compara el documento candidato contra las facturas de referencia.
    Usa similitud de Jaccard sobre los keywords fiscales encontrados.

    Args:
        filepath: Ruta al documento candidato.
        reference_fps: Lista de fingerprints de referencia.
        threshold: Umbral mínimo de similitud (0.0-1.0). Default 60%.

    Returns:
        True si al menos una referencia supera el umbral.
    """
    if not reference_fps:
        return False

    candidate_fp = _extract_fiscal_fingerprint(filepath)
    if not candidate_fp:
        return False

    for ref in reference_fps:
        ref_kw = ref["keywords"]
        # Similitud de Jaccard: |A ∩ B| / |A ∪ B|
        intersection = candidate_fp & ref_kw
        union = candidate_fp | ref_kw
        if not union:
            continue
        similarity = len(intersection) / len(union)

        if similarity >= threshold:
            log(f"  [REF-MATCH] Similitud {similarity:.0%} con '{ref['filename']}' "
                f"(comunes: {', '.join(sorted(intersection))})")
            return True

    # Log del mejor match si no superó el umbral
    best_sim = 0.0
    best_ref = ""
    for ref in reference_fps:
        ref_kw = ref["keywords"]
        intersection = candidate_fp & ref_kw
        union = candidate_fp | ref_kw
        if union:
            sim = len(intersection) / len(union)
            if sim > best_sim:
                best_sim = sim
                best_ref = ref["filename"]
    if best_ref:
        log(f"  [REF-NO-MATCH] Mejor similitud: {best_sim:.0%} con '{best_ref}' (umbral: {threshold:.0%})")

    return False


def validate_is_invoice(model: genai.GenerativeModel, filepath: str, sender_email: str = "", reference_fps: list | None = None, rejection: dict | None = None, email_date: datetime | None = None) -> dict | None:
    """
    Validación con jerarquía: Origen Email > Validación Fiscal > Contenido PDF.

    Envía el adjunto a Gemini para confirmar si es una factura real.
    - Si NO es factura -> devuelve None y, si se pasa `rejection`, anota en él el motivo
      ("kind": AI_NO, NO_TAX_ID, NO_DATE o AI_ERROR) y lo que dio la IA ("supplier", "date").
    - Si SÍ es factura -> devuelve {"supplier": "...", "date": "MM-YY"}.
    - Si hay error de API -> lanza GeminiAPIError.

    Paso A: Si el remitente es @amazon.* -> supplier forzado a 'AMAZON'.
    Paso B: Si no tiene CIF/NIF/VAT válido -> se descarta (excepto Amazon).
    Paso C: Parsear fecha y devolver datos.
    """
    filename = os.path.basename(filepath)
    is_amazon = _is_amazon_sender(sender_email)

    if is_amazon:
        log(f"  [PASO-A] Remitente Amazon detectado: {sender_email} -> Proveedor forzado a 'AMAZON'")

    log(f"  Validando con Gemini: {filename}")

    # Espera entre llamadas para respetar rate limits
    time.sleep(RATE_LIMIT_DELAY)

    # === Lógica de Validación Numérica y de Contexto ===
    forced_date = None
    forced_month_num = None
    forced_day_num = None
    month_written_with_day = False  # Fecha como "6 de marzo" o "27-Jun-2026": no admite dos lecturas
    pdf_text_extracted = False
    pdf_text_lower = ""
    
    if filepath.lower().endswith(".pdf"):
        try:
            with pdfplumber.open(filepath) as pdf:
                pdf_text_lower = " ".join(page.extract_text() or "" for page in pdf.pages).lower()

            pdf_text_extracted = bool(pdf_text_lower.strip())

            # 1. Regex Estricto (ej. 06/03/2026, 6-3-2026)
            for match in re.finditer(r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b', pdf_text_lower):
                try:
                    forced_date = datetime(int(match.group(3)), int(match.group(2)), int(match.group(1)))
                    break
                except ValueError:
                    continue  # Una fecha imposible no anula las siguientes

            # 2. Mapeo Matemático de Meses (completos + abreviados, ES + EN)
            if not forced_date:
                all_month_names = _MONTH_NAMES

                # 2a. Formato dd-Mon-yyyy / dd Mon yyyy (ej: 27-Jun-2026, 27 Jun 2026)
                abbrev_month_pattern = '|'.join(sorted(all_month_names.keys(), key=len, reverse=True))
                abbrev_match = re.search(
                    r'\b(\d{1,2})[\s\-/](' + abbrev_month_pattern + r')[\s\-/](\d{4})\b',
                    pdf_text_lower
                )
                if abbrev_match:
                    forced_day_num = int(abbrev_match.group(1))
                    forced_month_num = all_month_names[abbrev_match.group(2)]
                    y = int(abbrev_match.group(3))
                    if 1 <= forced_day_num <= 31 and 1 <= forced_month_num <= 12:
                        forced_date = datetime(y, forced_month_num, forced_day_num)
                        month_written_with_day = True
                        log(f"  [FECHA] Formato dd-Mon-yyyy detectado: {forced_day_num:02d}-{forced_month_num:02d}-{y}")

                # 2b. Formato "6 de marzo" (español con preposición)
                if not forced_date and forced_month_num is None:
                    month_match = re.search(
                        r'\b(\d{1,2})\.?\s+(?:de\s+)?(' + abbrev_month_pattern + r')\b',
                        pdf_text_lower
                    )
                    if month_match:
                        forced_day_num = int(month_match.group(1))
                        forced_month_num = all_month_names[month_match.group(2)]
                        month_written_with_day = True
                    else:
                        # "November 6, 2026" es cierta; "marzo 2026" fija solo el mes.
                        # Una palabra suelta ("may", "mar") no significa nada.
                        american = _AMERICAN_WRITTEN_DATE.search(pdf_text_lower)
                        month_year = _MONTH_AND_YEAR.search(pdf_text_lower)
                        if american:
                            forced_month_num = all_month_names[american.group(1)]
                            forced_day_num = int(american.group(2))
                            month_written_with_day = True
                        elif month_year:
                            forced_month_num = all_month_names[month_year.group(1)]
        except Exception as e:
            log(f"  [WARN] Fallo al extraer texto para validacion numerica: {e}")

    # Leer archivo y preparar inline_data
    ext = Path(filepath).suffix.lower()
    mime_type = MIME_MAP.get(ext, "application/octet-stream")
    with open(filepath, "rb") as f:
        file_bytes = f.read()
    file_part = {"inline_data": {"mime_type": mime_type, "data": file_bytes}}

    prompt = """Analiza este documento. ¿Es una factura real (invoice)?

Responde UNICAMENTE con un objeto JSON valido, sin markdown ni texto adicional.

{
  "is_invoice": "SI o NO",
  "supplier": "Nombre del proveedor o empresa que emite la factura",
  "tax_id": "CIF, NIF o VAT ID del emisor de la factura",
  "date": "Fecha de EMISION de la factura en formato YYYY-MM-DD (ejemplo: 2026-02-10 para el 10 de febrero de 2026), nunca la de vencimiento, pago, pedido, entrega, servicio o periodo",
  "date_kind": "Que fecha has puesto en 'date': EMISION si el documento la identifica como fecha de factura o de emision, VENCIMIENTO si es la de vencimiento o pago, OTRA si es de pedido, entrega, servicio o periodo, DESCONOCIDA si no lo sabes",
  "importe_total": "Importe TOTAL del documento, es decir la cantidad final a pagar con todos los impuestos ya sumados. Solo el numero con decimales y punto (ejemplo: 543.13). Toma el TOTAL final de la factura, no la base. Si no lo encuentras, usa null"
}

Reglas:
- "is_invoice": Responde estrictamente con "SI" si es una factura real con datos fiscales, o "NO" si es otro tipo de documento.
  DOCUMENTOS QUE NO SON FACTURAS (responder "NO"):
  * Solicitud de pago / Peticion de pago
  * Confirmacion de pedido
  * Confirmacion de envio
  * Albaran / Nota de entrega
  * Publicidad / Catalogo / Folleto
  * Presupuesto / Proforma (salvo que indique explicitamente "FACTURA")
  * Ticket de compra simple / Recibo / Justificante sin datos fiscales
  
  IMPORTANTE: Si el documento indica explicitamente "FACTURA" en su encabezado o titulo y contiene datos fiscales (CIF/NIF/VAT, base imponible, IVA) y una fecha de emision, es una factura real aunque internamente haga referencia a un ticket, recibo o numero de ticket. La palabra "ticket" como referencia interna NO invalida una factura. Sin embargo, si el documento NO contiene una fecha identificable, NO es una factura valida (responder "NO").

  REGLA DE TICKETS Y FACTURAS SIMPLIFICADAS (MUY IMPORTANTE): Si el documento tiene formato de ticket de caja, rollo de papel termico o escaneo fisico, pero contiene el titulo "FACTURA", "FACTURA NO.", "FACTURA Nº", "FACTURA SIMPLIFICADA" o incluye los datos del cliente junto al CIF/NIF del emisor y desglose de IVA/importes, DEBE considerarse una FACTURA VALIDA (responder "SI"). Esto aplica SIEMPRE, incluso si al final del documento aparece texto comercial o de devolucion como "los cambios se haran con el ticket de compra" o similar.

  REGLA FACTURA WEB (FW): Los documentos cuyo numero comienza por "FW" (Factura Web de EnvioMedical) NO son facturas validas (responder "NO"). EnvioMedical se gestiona por otro canal (portal B2B), sus correos se ignoran y sus documentos nunca deben entrar en Drive.

- "supplier": Solo si is_invoice es "SI". Nombre comercial o razon social de quien EMITE la factura.
  ** REGLA PRIORITARIA AMAZON **: Si la factura proviene de la plataforma Amazon (ya sea Amazon directamente, Amazon Business, Amazon Marketplace, o un vendedor externo que vende a traves de Amazon), el proveedor SIEMPRE debe ser "AMAZON". No uses el nombre del vendedor externo.

- "tax_id": Solo si is_invoice es "SI". El identificador fiscal del EMISOR de la factura. Busca el valor que acompana a cualquiera de estas etiquetas: IVA, VAT, CIF, NIF, ID de IVA, VAT ID, Numero de IVA, Tax ID, Numero de identificacion fiscal, NIF-IVA. En facturas de Amazon, el identificador fiscal suele estar debajo de la seccion 'Vendido por' con la etiqueta 'IVA'. Acepta formatos europeos e intracomunitarios (ej: ESB12345678, ESN0057922G, DE123456789, FR12345678901, IT12345678901, B12345678, N0057922G, Y1234567-Z, etc.). Devuelve el codigo completo tal como aparece en el documento, incluyendo el prefijo de pais si lo tiene. Si no encuentras ninguno, usa "DESCONOCIDO".

- "date": Solo si is_invoice es "SI". Fecha de la factura en formato YYYY-MM-DD (formato ISO). Ejemplo: 2026-02-10. Si ves un solo numero seguido de un mes (ej. 6 de marzo), el numero es el DIA. Nunca devuelvas un mes mayor al mes actual si el numero del dia es menor o igual a 12.
- Si no puedes determinar el proveedor, usa "DESCONOCIDO".
- Si no puedes determinar la fecha, usa la fecha actual en formato YYYY-MM-DD."""

    response = None
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = model.generate_content([file_part, prompt])
            last_error = None
            break  # Exito

        except Exception as e:
            error_str = str(e)
            last_error = e
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                # Extraer tiempo de espera sugerido o usar backoff exponencial
                wait = min(30 * attempt, 120)
                log(f"  Rate limit alcanzado (intento {attempt}/{MAX_RETRIES}), esperando {wait}s...", "WARN")
                time.sleep(wait)
            else:
                # Error no recuperable
                break

    # Si todos los reintentos fallaron
    if last_error is not None:
        raise GeminiAPIError(f"Gemini API error tras {MAX_RETRIES} intentos: {last_error}")

    try:
        # Limpiar la respuesta de posible markdown
        text = response.text.strip()
        text = re.sub(r'^```json\s*', '', text)
        text = re.sub(r'\s*```$', '', text)

        result = json.loads(text)

        # -- Filtro: Es factura? --
        is_invoice_str = (result.get("is_invoice") or "NO").strip().upper()
        if is_invoice_str not in ("SI", "YES"):
            # Rescate 1: Si es un documento escaneado (sin texto digital extraíble), re-verificar visualmente
            if not pdf_text_extracted and filepath.lower().endswith(tuple(INVOICE_EXTENSIONS)):
                rescue_prompt = """Revisa cuidadosamente este documento escaneado/imagen.
¿Contiene algún indicio fiscal de factura como 'Factura', 'Factura Simplificada', 'Factura Nº', CIF/NIF (ej: B..., A..., X...), datos de cliente o desglose de IVA/base imponible?
Responde estrictamente con un objeto JSON:
{
  "is_invoice": "SI o NO",
  "supplier": "Nombre comercial o razon social del emisor",
  "tax_id": "CIF, NIF o VAT ID del emisor",
  "date": "YYYY-MM-DD"
}"""
                try:
                    time.sleep(RATE_LIMIT_DELAY)
                    rescue_response = model.generate_content([file_part, rescue_prompt])
                    rescue_text = rescue_response.text.strip()
                    rescue_text = re.sub(r'^```json\s*', '', rescue_text)
                    rescue_text = re.sub(r'\s*```$', '', rescue_text)
                    rescue_result = json.loads(rescue_text)
                    if rescue_result.get("is_invoice", "NO").strip().upper() in ("SI", "YES"):
                        log(f"  [RESCATE-SCAN] Re-evaluación visual confirmó que es Factura/Ticket fiscal: {filename}")
                        result = rescue_result
                        is_invoice_str = "SI"
                except Exception as e:
                    log(f"  [RESCATE-SCAN] No se pudo re-evaluar documento escaneado: {e}", "WARN")

        if is_invoice_str not in ("SI", "YES"):
            # Rescate 2: comparar con facturas de referencia
            if reference_fps and _matches_reference_invoice(filepath, reference_fps):
                log(f"  [RESCATE] Gemini dijo NO, pero el documento coincide con una factura de referencia. Continuando...")
            else:
                log(f"  [NO] No es factura -- descartado: {filename}")
                if rejection is not None:
                    rejection.update(kind="AI_NO", supplier=result.get("supplier"), date=result.get("date"))
                return None

        # -- Extraer datos crudos de Gemini --
        supplier = (result.get("supplier") or "DESCONOCIDO").strip()
        tax_id   = (result.get("tax_id") or "DESCONOCIDO").strip()
        raw_date = (result.get("date") or "").strip()

        # Log del valor crudo de CIF/VAT que devuelve la IA
        log(f"  [DEBUG] Valor CIF/VAT devuelto por IA: '{tax_id}'")

        # ── PASO B: Filtro CIF/NIF/VAT (obligatorio para TODOS) ──
        tax_id_valid = _is_valid_tax_id(tax_id)

        # Plan B: si la IA no detectó CIF, intentar extracción directa del PDF
        if not tax_id_valid:
            log(f"  [FALLBACK] IA devolvió '{tax_id}', intentando extracción directa del PDF...")
            fallback_id = _extract_tax_id_from_pdf(filepath)
            if fallback_id:
                tax_id = fallback_id
                tax_id_valid = _is_valid_tax_id(tax_id)
        # ── Consultar Regla Aprendida (Feedback Loop) ──
        vendor_rule = None
        try:
            import data_manager
            vendor_rule = data_manager.get_vendor_rule(supplier) or data_manager.get_vendor_rule(sender_email)
        except Exception:
            pass

        if not tax_id_valid and vendor_rule and vendor_rule.get("always_accept"):
            log(f"  [REGLA-APRENDIDA] Proveedor '{supplier}' tiene 'always_accept=True'. Validación fiscal forzada.")
            tax_id = vendor_rule.get("tax_id") or "ACEPTADO-POR-REGLA"
            tax_id_valid = True

        if not tax_id_valid:
            # Último recurso: comparar con facturas de referencia
            if reference_fps and _matches_reference_invoice(filepath, reference_fps):
                log(f"  [RESCATE] Sin CIF detectado, pero el documento coincide con una factura de referencia. Continuando...")
                tax_id = "RESCATADO-REF"
            else:
                if is_amazon:
                    log(f"  [DEBUG] Documento de Amazon descartado: No se detecta CIF/NIF legal.")
                else:
                    log(f"  [FILTRO-CIF] Sin CIF/NIF/VAT detectado en '{filename}' -> Descartado")
                if rejection is not None:
                    rejection.update(kind="NO_TAX_ID", supplier=supplier, date=raw_date)
                return None
        else:
            log(f"  [CIF] Identificacion fiscal detectada: {tax_id}")

        # ── PASO D: Filtro de fecha verificable (solo PDFs con texto extraíble) ──
        if forced_date is None and forced_month_num is None and pdf_text_extracted:
            has_any_date = False

            # Patrones numéricos de fecha (dd/mm/yyyy, yyyy-mm-dd, dd.mm.yyyy, etc.)
            date_patterns = [
                r'\b\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}\b',
                r'\b\d{4}[/\-\.]\d{1,2}[/\-\.]\d{1,2}\b',
            ]
            for pat in date_patterns:
                if re.search(pat, pdf_text_lower):
                    has_any_date = True
                    break

            # Meses en español e inglés (completos y abreviados)
            if not has_any_date:
                if re.search(rf'\b(?:{_MONTH_PATTERN})\b', pdf_text_lower):
                    has_any_date = True

            if not has_any_date:
                # Último recurso: comparar con facturas de referencia
                if reference_fps and _matches_reference_invoice(filepath, reference_fps):
                    log(f"  [RESCATE] Sin fecha verificable, pero el documento coincide con una factura de referencia. Continuando...")
                else:
                    log(f"  [FILTRO-FECHA] Sin fecha verificable en '{filename}' -> Descartado (posible ticket/recibo)")
                    if rejection is not None:
                        rejection.update(kind="NO_DATE", supplier=supplier, date=raw_date)
                    return None
            else:
                log(f"  [FECHA-OK] Fecha detectada en el texto del documento.")

        # ── PASO A: Override Amazon por remitente (solo si superó el filtro CIF) ──
        if is_amazon:
            supplier = "AMAZON"
            log(f"  [PASO-A] Proveedor forzado a 'AMAZON' (remitente blindado)")

        # ── Normalización de Proveedores / Alias (Razón Social -> Nombre Comercial) ──
        cleaned_tax_id = re.sub(r'[\s\.\-]', '', tax_id.upper())
        if cleaned_tax_id in TAX_ID_SUPPLIER_MAP:
            mapped_supplier = TAX_ID_SUPPLIER_MAP[cleaned_tax_id]
            log(f"  [PROVEEDOR-ALIAS] Proveedor asignado por CIF ({tax_id}): '{mapped_supplier}'")
            supplier = mapped_supplier
        else:
            supplier_check = supplier.upper().strip()
            for alias_key, alias_val in SUPPLIER_ALIASES.items():
                if alias_key in supplier_check or supplier_check in alias_key:
                    log(f"  [PROVEEDOR-ALIAS] Proveedor '{supplier}' normalizado a '{alias_val}'")
                    supplier = alias_val
                    break

        # ── PASO C: Parsear fecha (formato primario ISO YYYY-MM-DD, fallback europeo) ──
        ai_date_read = False
        try:
            if raw_date:
                # Intentar formato ISO estricto primero (el solicitado a la IA)
                try:
                    dt = datetime.strptime(raw_date, "%Y-%m-%d")
                except ValueError:
                    dt = dateutil_parser.parse(raw_date, dayfirst=True).replace(tzinfo=None)
                ai_date_read = True
            else:
                dt = datetime.now()
        except (ValueError, OverflowError):
            log(f"  Fecha no parseable: '{raw_date}', usando fecha actual.", "WARN")
            dt = datetime.now()

        # La IA elige cuál es la fecha de la factura; el texto la confirma o corrige. Solo se
        # cambia por otra si hay una razón clara; si hay varias posibles, decide el usuario.
        date_doubt = False
        date_from_invoice_label = False
        text_dates = _find_text_dates(pdf_text_lower)
        other_dates = _labelled_dates(pdf_text_lower, _OTHER_DATE_LABELS)
        if ai_date_read:
            confirmed = next((d for d in text_dates if _same_date(d, dt)), None)
            swapped = next((d for d in text_dates if d["strict"] and d["day"] == dt.month
                            and d["month"] == dt.day and d["year"] == dt.year), None)
            ai_is_other_date = any(_same_date(d, dt) for d in other_dates)

            # Una etiqueta junto a la fecha es lo más fiable: "Fecha de factura: 03/04/2026"
            invoice_labelled = _labelled_dates(pdf_text_lower, _INVOICE_DATE_LABELS)
            bare_labelled = _labelled_dates(pdf_text_lower, _BARE_DATE_LABELS)
            label_choice = None
            keep_ai_date = False
            if len({(d["day"], d["month"]) for d in invoice_labelled}) > 1:
                date_doubt = keep_ai_date = True  # Dos fechas de factura distintas
            elif invoice_labelled:
                label_choice = invoice_labelled[0]
            elif bare_labelled and (not confirmed or ai_is_other_date):
                label_choice = bare_labelled[0]
            elif bare_labelled and not any(_same_date(d, dt) for d in bare_labelled):
                date_doubt = keep_ai_date = True  # La IA y la etiqueta "Fecha:" dicen cosas distintas

            if label_choice:
                if not _same_date(label_choice, dt):
                    log(f"  [FECHA-ETIQUETA] La IA leyó {dt.strftime('%d/%m/%Y')}, pero la etiqueta de la factura dice {label_choice['day']:02d}/{label_choice['month']:02d}.")
                dt = datetime(label_choice["year"] or dt.year, label_choice["month"], label_choice["day"])
                forced_date, forced_month_num, forced_day_num = None, None, None
                date_from_invoice_label = True
            elif keep_ai_date or confirmed:
                forced_date, forced_month_num, forced_day_num = None, None, None
                if confirmed:
                    log(f"  [VERIFICACIÓN-NUMÉRICA] El texto confirma la fecha de la IA: {dt.strftime('%d/%m/%Y')}")
            elif swapped:
                forced_date = datetime(swapped["year"], swapped["month"], swapped["day"])
                forced_month_num, forced_day_num = None, None
            else:
                possible = set()
                for d in text_dates:
                    try:
                        datetime(d["year"] or dt.year, d["month"], d["day"])
                        possible.add((d["day"], d["month"]))
                    except ValueError:
                        pass
                if len(possible) > 1:
                    # Varias fechas y ninguna es la de la IA: no se sustituye a ciegas
                    log(f"  [DUDA-FECHA] {dt.strftime('%d/%m/%Y')} no coincide con ninguna de las {len(possible)} fechas del documento.", "WARN")
                    forced_date, forced_month_num, forced_day_num = None, None, None
                    date_doubt = keep_ai_date = True

            if label_choice or keep_ai_date or confirmed or swapped:
                # Es cierta si esa misma fecha aparece con el mes en letras
                month_written_with_day = any(d["written"] and _same_date(d, dt) for d in text_dates)

            if str(result.get("date_kind") or "").strip().upper() in ("VENCIMIENTO", "OTRA") and not date_from_invoice_label:
                log(f"  [DUDA-FECHA] La IA indica que {dt.strftime('%d/%m/%Y')} no es la fecha de emisión.", "WARN")
                date_doubt = True

        # Aplicar Reglas de Verificacion y Mapeo:
        if forced_date:
            dt = forced_date
            log(f"  [VERIFICACIÓN-NUMÉRICA] Día: {dt.day:02d}, Mes: {dt.month:02d}. Confirmado por Regex Estricto.")
        elif forced_month_num is not None:
            log_str = "Confirmado por contexto español."
            if forced_day_num is not None:
                try:
                    dt = dt.replace(month=forced_month_num, day=forced_day_num)
                    log(f"  [VERIFICACIÓN-NUMÉRICA] Día: {forced_day_num:02d}, Mes: {forced_month_num:02d}. {log_str}")
                except ValueError:
                    dt = dt.replace(month=forced_month_num)
                    log(f"  [VERIFICACIÓN-NUMÉRICA] Día: {dt.day:02d}, Mes: {forced_month_num:02d}. {log_str}")
            else:
                try:
                    dt = dt.replace(month=forced_month_num)
                    log(f"  [VERIFICACIÓN-NUMÉRICA] Día: {dt.day:02d}, Mes: {forced_month_num:02d}. {log_str}")
                except ValueError:
                    pass

        # Aplicar formato de fecha aprendido si existe
        has_learned_date_format = bool(vendor_rule and vendor_rule.get("date_format"))
        if has_learned_date_format:
            fmt = vendor_rule["date_format"].upper()
            if fmt == "MM/DD/YYYY" and dt.day <= 12 and dt.month <= 12 and not month_written_with_day:
                try:
                    dt = dt.replace(month=dt.day, day=dt.month)
                    log(f"  [REGLA-FECHA] Formato MM/DD/YYYY aplicado para '{supplier}': {dt.strftime('%d/%m/%Y')}")
                except ValueError:
                    pass
            elif fmt == "DD/MM/YYYY":
                log(f"  [REGLA-FECHA] Formato DD/MM/YYYY confirmado para '{supplier}': {dt.strftime('%d/%m/%Y')}")

        # Filtro de Seguridad Automático (Fecha Futura)
        now = datetime.now()
        if dt > now:
            log(f"  [SEGURIDAD] Fecha futura detectada ({dt.strftime('%Y-%m-%d')}). Invirtiendo día y mes automáticamente.")
            if not month_written_with_day:
                try:
                    dt = dt.replace(month=dt.day, day=dt.month)
                except ValueError:
                    pass

            if dt > now:
                dt = now

        # Detección de Ambigüedad (ambos <= 12, diferentes y sin regla aprendida).
        # No se pregunta si el mes va con letras, ni si el emisor es español y el
        # propio texto confirma la fecha como día/mes.
        date_is_certain = month_written_with_day or (
            _is_spanish_tax_id(tax_id) and _text_confirms_day_first(pdf_text_lower, dt)
        )

        # También se pregunta, aunque haya regla aprendida, si la fecha no puede ser la
        # de la factura: es un vencimiento u otra fecha etiquetada, o es posterior al correo.
        if not date_from_invoice_label and any(_same_date(d, dt) for d in other_dates) \
                and any(not _same_date(d, dt) for d in text_dates):
            log(f"  [DUDA-FECHA] {dt.strftime('%d/%m/%Y')} parece un vencimiento u otra fecha, no la de la factura.", "WARN")
            date_doubt = True
        if email_date and dt.date() > email_date.date() + timedelta(days=1):
            log(f"  [DUDA-FECHA] {dt.strftime('%d/%m/%Y')} es posterior al correo ({email_date.strftime('%d/%m/%Y')}).", "WARN")
            date_doubt = True

        can_swap = dt.day <= 12 and dt.month <= 12 and dt.day != dt.month
        is_ambiguous = False
        ambiguous_options = []
        if date_doubt or (not has_learned_date_format and not date_is_certain and can_swap):
            is_ambiguous = True
            _MONTH_NAMES_ES = {
                1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
                5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
                9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
            }
            ambiguous_options = [
                {
                    "day": dt.day,
                    "month": dt.month,
                    "year": dt.year,
                    "format": "DD/MM/YYYY",
                    "folder": f"{dt.month:02d}-{str(dt.year)[-2:]}",
                    "label": f"{dt.day} de {_MONTH_NAMES_ES.get(dt.month, '')} de {dt.year}",
                },
                {
                    "day": dt.month,
                    "month": dt.day,
                    "year": dt.year,
                    "format": "MM/DD/YYYY",
                    "folder": f"{dt.day:02d}-{str(dt.year)[-2:]}",
                    "label": f"{dt.month} de {_MONTH_NAMES_ES.get(dt.day, '')} de {dt.year}",
                }
            ]
            if not can_swap:
                ambiguous_options = ambiguous_options[:1]  # La lectura invertida no sería una fecha real
            log(f"  [AMBIGÜEDAD] Fecha ambigua detectada ({dt.day}/{dt.month} vs {dt.month}/{dt.day}) para '{supplier}'")

        date_str = dt.strftime("%m-%y")

        # Doble comprobación: log del día, mes y carpeta destino
        log(f"  [FECHA] Procesando dia: {dt.strftime('%d')}, mes: {dt.strftime('%m')}. Carpeta destino: {date_str}")

        # Importe total del documento (para el historial del panel)
        amount_val = None
        raw_amount = result.get("importe_total")
        if raw_amount not in (None, "", "null"):
            try:
                amount_val = float(str(raw_amount).replace("€", "").replace(" ", "").replace(",", "."))
            except (ValueError, TypeError):
                amount_val = None
        if amount_val is not None:
            log(f"  [IMPORTE] Total del documento: {amount_val:.2f} EUR")

        log(f"  [SI] Factura confirmada -- Proveedor: {supplier} | CIF: {tax_id} | Fecha: {date_str}")
        return {
            "supplier": supplier,
            "date": date_str,
            "amount": amount_val,
            "is_ambiguous": is_ambiguous,
            "ambiguous_options": ambiguous_options,
        }

    except (ValueError, KeyError, AttributeError, TypeError) as e:
        # Respuesta bloqueada o con formato inesperado: reintentar daria lo mismo,
        # asi que el documento va a 'Descartadas', donde el usuario puede rescatarlo.
        log(f"  Error al interpretar la respuesta de Gemini: {e}", "ERROR")
        if rejection is not None:
            rejection["kind"] = "AI_ERROR"
        return None


# ── Google Drive ──────────────────────────────────────────────────────────────

def authenticate_drive():
    """
    Autentica con Google Drive API usando OAuth2.
    La primera vez abrirá un navegador para autorizar.
    Después, usa el token guardado en token.json.
    """
    creds = None
    script_dir = os.path.dirname(os.path.abspath(__file__))
    token_path = os.path.join(script_dir, "token.json")
    creds_path = os.path.join(script_dir, "credentials.json")

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, DRIVE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log("Renovando token de Google Drive...")
            creds.refresh(Request())
        else:
            if not os.path.exists(creds_path):
                log("No se encontro 'credentials.json'. Sigue las instrucciones del README.", "ERROR")
                log(f"  Ruta esperada: {creds_path}", "ERROR")
                sys.exit(1)
            log("Iniciando autorizacion de Google Drive (se abrira el navegador)...")
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, DRIVE_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
        log("Token de Drive guardado.")

    service = build("drive", "v3", credentials=creds)
    log("Google Drive autenticado.")
    return service


def get_or_create_folder(service, name: str, parent_id: str) -> str:
    """
    Busca una carpeta por nombre EXACTO dentro de `parent_id`.
    Usa corpora='allDrives' para encontrar carpetas en unidades compartidas.
    Si hay duplicados, devuelve la primera. Solo crea si no existe ninguna.
    """
    log(f"  [DRIVE] Buscando carpeta '{name}' en parent {parent_id}...")

    query = (
        f"name = '{name}' and "
        f"'{parent_id}' in parents and "
        f"mimeType = 'application/vnd.google-apps.folder' and "
        f"trashed = false"
    )

    results = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        pageSize=10,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute()

    found = results.get("files", [])

    if found:
        chosen = found[0]
        extra = f" ({len(found)} duplicados, usando la primera)" if len(found) > 1 else ""
        log(f"  [DRIVE] Carpeta '{chosen['name']}' encontrada con ID {chosen['id']}{extra}")
        return chosen["id"]

    # No existe -> crear
    log(f"  [DRIVE] Carpeta '{name}' no encontrada, creando nueva...")
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(
        body=metadata, fields="id",
        supportsAllDrives=True,
    ).execute()
    log(f"  [DRIVE] Carpeta '{name}' creada con ID {folder['id']}")
    return folder["id"]


def _normalize_name(name: str) -> str:
    """
    Normaliza un nombre de proveedor para comparaciones:
    - Convierte a mayúsculas
    - Quita acentos/diacríticos (á->A, é->E, ñ->N, etc.)
    - Quita sufijos legales comunes (S.L., S.A., S.L.U., LIMITED, LTD, INC, etc.)
    - Elimina signos de puntuación (. , - _ : ;)
    - Colapsa espacios múltiples y recorta
    """
    # Mayúsculas
    name = name.upper().strip()
    # Quitar acentos: descomponer + eliminar combining marks
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    # Quitar sufijos legales comunes
    suffixes = [
        r"\bS\.?L\.?U?\.?\b", r"\bS\.?A\.?\b", r"\bLIMITED\b",
        r"\bLTD\.?\b", r"\bINC\.?\b", r"\bS\.?C\.?P?\.?\b",
        r"\bCOOP\.?\b", r"\bGMBH\b",
    ]
    for pat in suffixes:
        name = re.sub(pat, "", name)
    # Eliminar puntuación
    name = re.sub(r"[.,\-_:;()/]", " ", name)
    # Colapsar espacios
    name = re.sub(r"\s+", " ", name).strip()
    return name


def get_or_create_supplier_folder(service, supplier_name: str, parent_id: str) -> str:
    """
    Busca una carpeta de proveedor con match inteligente dentro de `parent_id`.
    Todas las comparaciones se hacen sobre nombres NORMALIZADOS (sin acentos,
    sin puntuación, sin sufijos legales).

    Estrategia (en orden de prioridad):
      1. Match EXACTO normalizado.
      2. Match PARCIAL: nombre normalizado contenido en el otro.
      3. Match por PALABRA CLAVE (>= 3 chars) normalizada.

    Si no hay match, crea una carpeta nueva en MAYUSCULAS.
    """
    # Comprobar alias configurados
    for alias_key, alias_val in SUPPLIER_ALIASES.items():
        if _normalize_name(alias_key) == _normalize_name(supplier_name) or _normalize_name(alias_key) in _normalize_name(supplier_name):
            log(f"  [DRIVE-ALIAS] Proveedor '{supplier_name}' mapeado a alias '{alias_val}'")
            supplier_name = alias_val
            break

    supplier_upper = supplier_name.upper().strip()
    supplier_norm = _normalize_name(supplier_name)
    log(f"  [DRIVE] Buscando proveedor '{supplier_upper}' (normalizado: '{supplier_norm}') en parent {parent_id}...")

    # Listar TODAS las subcarpetas del mes
    query = (
        f"'{parent_id}' in parents and "
        f"mimeType = 'application/vnd.google-apps.folder' and "
        f"trashed = false"
    )
    results = service.files().list(
        q=query,
        spaces="drive",
        fields="files(id, name)",
        pageSize=200,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        corpora="allDrives",
    ).execute()

    existing = results.get("files", [])
    if existing:
        log(f"  [DRIVE] {len(existing)} carpeta(s) existente(s): "
            + ", ".join(f"'{f['name']}'" for f in existing))
    else:
        log(f"  [DRIVE] 0 carpetas existentes en este mes")

    # Pre-calcular nombres normalizados
    normed = [(folder, _normalize_name(folder["name"])) for folder in existing]

    # 1. Match exacto normalizado
    for folder, fn in normed:
        if fn == supplier_norm:
            log(f"  [DRIVE] Proveedor match EXACTO: '{folder['name']}' con ID {folder['id']}")
            return folder["id"]

    # 2. Match parcial normalizado (contenido)
    for folder, fn in normed:
        if fn and supplier_norm and (fn in supplier_norm or supplier_norm in fn):
            log(f"  [DRIVE] Proveedor match PARCIAL: '{folder['name']}' con ID {folder['id']}")
            return folder["id"]

    # 3. Match por palabra clave normalizada
    supplier_words = {w for w in supplier_norm.split() if len(w) >= 3}
    for folder, fn in normed:
        folder_words = {w for w in fn.split() if len(w) >= 3}
        common = supplier_words & folder_words
        if common:
            log(f"  [DRIVE] Proveedor match PALABRA {common}: '{folder['name']}' con ID {folder['id']}")
            return folder["id"]

    # Sin match -> crear nueva en MAYUSCULAS
    log(f"  [DRIVE] Proveedor '{supplier_upper}' no encontrado, creando nueva...")
    metadata = {
        "name": supplier_upper,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(
        body=metadata, fields="id",
        supportsAllDrives=True,
    ).execute()
    log(f"  [DRIVE] Proveedor '{supplier_upper}' creado con ID {folder['id']}")
    return folder["id"]


def get_file_md5(filepath: str) -> str:
    """Calcula el MD5 de un archivo local en fragmentos."""
    hash_md5 = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

def upload_to_drive(service, filepath: str, folder_id: str) -> str | None:
    """
    Sube un archivo a una carpeta de Drive. Devuelve el ID.
    Validación Inteligente de Duplicados: comprueba MD5 si el nombre ya existe.
    """
    original_filename = os.path.basename(filepath)
    local_md5 = get_file_md5(filepath)
    
    filename = original_filename
    counter = 1
    
    while True:
        # Comprobar si el archivo ya existe en la carpeta
        dup_query = (
            f"name = '{filename}' and "
            f"'{folder_id}' in parents and "
            f"trashed = false"
        )
        dup_results = service.files().list(
            q=dup_query,
            spaces="drive",
            fields="files(id, name, md5Checksum)",
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
        ).execute()

        found_files = dup_results.get("files", [])
        
        if not found_files:
            # El nombre está libre, salimos del bucle para subirlo
            break
            
        # El nombre ya existe, comprobamos el contenido real (MD5) de la nube
        drive_file = found_files[0]
        drive_md5 = drive_file.get("md5Checksum", "")
        
        if drive_md5 == local_md5:
            log(f"  [DUPLICADO] El contenido es exactamente igual. Saltando...")
            return None
            
        # Contenido distinto, renombramos recursivamente
        base, ext = os.path.splitext(original_filename)
        filename = f"{base} ({counter}){ext}"
        log(f"  [COLISIÓN] Nombre repetido pero contenido distinto. Renombrando a: {filename}")
        counter += 1

    # Detectar MIME type
    ext = Path(filepath).suffix.lower()
    mime_map = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".tiff": "image/tiff",
        ".webp": "image/webp",
    }
    mime_type = mime_map.get(ext, "application/octet-stream")

    metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(filepath, mimetype=mime_type, resumable=True)
    file = service.files().create(
        body=metadata, media_body=media, fields="id",
        supportsAllDrives=True,
    ).execute()

    log(f"  [DRIVE] Subido: {filename} ({file['id']})")
    return file["id"]


def move_drive_file(service, file_id: str, old_parent_id: str, new_parent_id: str) -> bool:
    """Mueve un archivo en Google Drive de una carpeta a otra sin duplicarlo."""
    try:
        service.files().update(
            fileId=file_id,
            addParents=new_parent_id,
            removeParents=old_parent_id,
            fields="id, parents",
            supportsAllDrives=True,
        ).execute()
        log(f"  [DRIVE] Archivo {file_id} movido exitosamente a carpeta {new_parent_id}")
        return True
    except Exception as e:
        log(f"  [DRIVE-ERROR] Error al mover archivo {file_id}: {e}", "ERROR")
        return False


# ── Flujo principal ──────────────────────────────────────────────────────────

def trash_drive_file(service, file_id: str) -> bool:
    """Envía un archivo a la papelera de Drive, recuperable. Si ya no existe, cuenta como retirado."""
    try:
        service.files().update(
            fileId=file_id,
            body={"trashed": True},
            fields="id",
            supportsAllDrives=True,
        ).execute()
        log(f"  [DRIVE] Archivo {file_id} enviado a la papelera")
        return True
    except Exception as e:
        if getattr(getattr(e, "resp", None), "status", None) == 404:
            log(f"  [DRIVE] El archivo {file_id} ya no estaba en Drive")
            return True
        log(f"  [DRIVE-ERROR] Error al enviar a la papelera {file_id}: {e}", "ERROR")
        return False


def _finish_email(mail: imaplib.IMAP4_SSL, uid: str, msg: email.message.Message) -> None:
    """Marca el correo como leído y lo anota como revisado para no repetirlo."""
    mark_as_read(mail, uid)
    try:
        import data_manager
        data_manager.remember_processed_email(email_key(msg.get("Message-ID"), uid))
    except Exception as e:
        log(f"  [WARN] No se pudo anotar el correo como revisado: {e}", "WARN")


def _register_rejected_document(filepath: str, sender: str, subject: str, rejection: dict) -> None:
    """Decide adónde va un documento rechazado: se ignora, a Dudosas o a Descartadas."""
    import data_manager

    filename = os.path.basename(filepath)
    kind = rejection.get("kind")
    today_folder = datetime.now().strftime("%m-%y")

    if kind != "AI_ERROR" and data_manager.is_rejected_format(sender, filename):
        log(f"  [IGNORADO] '{filename}' tiene un formato que ya se sabe que no es factura.")
        data_manager.add_history_entry(
            status="IGNORED_KNOWN_FORMAT",
            filename=filename,
            supplier=sender or "DESCONOCIDO",
            date_str=today_folder,
            reason="Formato de documento que no es factura",
            email_subject=subject,
            sender=sender,
        )
        return

    if kind in ("NO_TAX_ID", "NO_DATE") and data_manager.is_known_supplier(sender, rejection.get("supplier")):
        missing = "el CIF" if kind == "NO_TAX_ID" else "la fecha"
        log(f"  [DUDOSA] ⚠️ '{filename}' es de un proveedor conocido pero no se encuentra {missing} -> A revisar en 'Facturas Dudosas'.", "WARN")
        suggested = rejection.get("date")
        data_manager.add_history_entry(
            status="REVIEW_MISSING_DATA",
            filename=filename,
            supplier=rejection.get("supplier") or sender or "DESCONOCIDO",
            date_str=today_folder,
            reason=f"Proveedor conocido, pero no se encuentra {missing} en el documento.",
            original_filepath=filepath,
            email_subject=subject,
            sender=sender,
            detected_dates=[{"iso": suggested}] if suggested else [],
        )
        return

    log(f"  [DESCARTADA] 🚫 Documento '{filename}' de {sender} no es factura válida o falta CIF -> Disponible en 'Descartadas' para revisar o rescatar.", "INFO")
    data_manager.add_history_entry(
        status="DISCARDED",
        filename=filename,
        supplier=sender or "DESCONOCIDO",
        date_str=today_folder,
        reason="No clasificado como factura válida o sin identificación fiscal (CIF/NIF/VAT)",
        original_filepath=filepath,
        email_subject=subject,
        sender=sender,
    )


def process_invoice(
    mail: imaplib.IMAP4_SSL,
    uid: str,
    gemini_client: genai.GenerativeModel,
    drive_service,
    tmp_dir: str,
    reference_fps: list | None = None,
) -> int:
    """
    Procesa un unico correo: extraer adjuntos -> validar -> subir -> marcar leido.

    Devuelve cuantos documentos fallaron y deben reintentarse. Si hay alguno, el
    correo sigue sin leer. Todo correo procesado deja rastro en el historial.
    """

    msg = fetch_email(mail, uid)
    subject = decode_mime_header(msg.get("Subject"))
    sender  = decode_mime_header(msg.get("From"))
    log(f"\n{'='*60}")
    log(f"Procesando: {subject}")
    log(f"   De: {sender}")

    # 0. Proveedores ignorados: sus facturas llegan por otro canal.
    #    Se marcan como leidos para no volver a analizarlos.
    ignored_domain = _is_ignored_sender(sender)
    if ignored_domain:
        log(f"  [IGNORADO] Remitente de '{ignored_domain}': sus facturas se descargan del portal B2B, correo omitido.")
        _finish_email(mail, uid, msg)
        return 0

    # 1. Extraer adjuntos
    attachments = extract_attachments(msg, tmp_dir)
    if not attachments:
        ignored_names = [
            decode_mime_header(p.get_filename()) for p in msg.walk() if p.get_filename()
        ]
        ignored_text = ", ".join(ignored_names) if ignored_names else "ninguno"
        log(f"  [SIN DOCUMENTO] El correo '{subject}' no trae ningun PDF ni imagen util. Adjuntos ignorados: {ignored_text}", "WARN")
        try:
            import data_manager
            data_manager.add_history_entry(
                status="NO_DOCUMENT",
                filename=ignored_names[0] if ignored_names else "(sin adjuntos)",
                supplier=sender or "DESCONOCIDO",
                date_str=datetime.now().strftime("%m-%y"),
                reason=f"El correo no trae ningun PDF ni imagen util. Adjuntos ignorados: {ignored_text}",
                email_subject=subject,
                sender=sender,
            )
        except Exception as e_dm:
            log(f"  [ERROR] No se pudo registrar el correo sin documento: {e_dm}", "ERROR")
            return 1
        _finish_email(mail, uid, msg)
        return 0

    # 2. Procesar cada adjunto
    any_invoice_found = False
    failed_count = 0
    for filepath in attachments:
        try:
            # 2a. Validar con Gemini (anti-falsos positivos)
            rejection = {}
            info = validate_is_invoice(
                gemini_client, filepath, sender_email=sender, reference_fps=reference_fps,
                rejection=rejection, email_date=_email_datetime(msg.get("Date")),
            )
            if info is None:
                try:
                    _register_rejected_document(filepath, sender, subject, rejection)
                except Exception as e_dm:
                    # Sin registro ni cuarentena el documento se perderia: reintentar
                    log(f"  [ERROR] Fallo al registrar descarte: {e_dm}", "ERROR")
                    failed_count += 1
                continue

            any_invoice_found = True
            supplier_name = sanitize_folder_name(info["supplier"])
            date_str      = info["date"]  # formato MM-YY

            # 2b. Derivar año y mes de la fecha MM-YY
            parts = date_str.split("-")
            if len(parts) == 2:
                month_part, year_short = parts
                year_folder = f"20{year_short}"  # ej: "26" -> "2026"
            else:
                year_folder = datetime.now().strftime("%Y")
                month_part = datetime.now().strftime("%m")
                year_short = datetime.now().strftime("%y")
                date_str = f"{month_part}-{year_short}"

            month_folder_name = date_str  # ej: "01-26"

            # 2c. Crear estructura jerárquica en Drive:
            #     Root -> Año -> MM-YY -> Proveedor
            log(f"  Estructura: {year_folder} / {month_folder_name} / {supplier_name}")

            year_folder_id = get_or_create_folder(
                drive_service, year_folder, DRIVE_ROOT_ID
            )
            month_folder_id = get_or_create_folder(
                drive_service, month_folder_name, year_folder_id
            )
            supplier_folder_id = get_or_create_supplier_folder(
                drive_service, supplier_name, month_folder_id
            )

            # 2d. Subir archivo
            drive_file_id = upload_to_drive(drive_service, filepath, supplier_folder_id)
            is_ambiguous = info.get("is_ambiguous", False)
            folder_path_str = f"{year_folder} / {month_folder_name} / {supplier_name}"

            # Si upload_to_drive devuelve None, es un duplicado exacto (mismo nombre
            # y mismo contenido MD5 ya presentes en Drive). Se registra con HONESTIDAD:
            # NO cuenta como subida nueva.
            try:
                import data_manager
                if drive_file_id is None:
                    log(f"  [DUPLICADO] ⏭️ Factura '{os.path.basename(filepath)}' de {supplier_name} ya estaba en Google Drive ({folder_path_str}). No se ha vuelto a subir.", "WARN")
                    data_manager.add_history_entry(
                        status="SKIPPED_DUPLICATE",
                        filename=os.path.basename(filepath),
                        supplier=supplier_name,
                        date_str=date_str,
                        drive_file_id=None,
                        drive_folder_id=supplier_folder_id,
                        drive_folder_path=folder_path_str,
                        original_filepath=filepath,
                        email_subject=subject,
                        sender=sender,
                        detected_dates=info.get("ambiguous_options", []),
                        amount=info.get("amount"),
                        reason="Ya existía en Drive (mismo nombre y contenido MD5). Subida omitida.",
                    )
                else:
                    if is_ambiguous:
                        log(f"  [DUDOSA] ⚠️ Factura '{os.path.basename(filepath)}' de {supplier_name} con fecha ambigua -> Guardada provisionalmente en '{folder_path_str}' y añadida a 'Facturas Dudosas'.", "WARN")
                    else:
                        log(f"  [SUCCESS] [DRIVE] ✅ Factura archivada con éxito: '{os.path.basename(filepath)}' de {supplier_name} -> Carpeta: {folder_path_str}", "SUCCESS")

                    status_to_record = "AMBIGUOUS_DATE" if is_ambiguous else "SUCCESS"
                    data_manager.add_history_entry(
                        status=status_to_record,
                        filename=os.path.basename(filepath),
                        supplier=supplier_name,
                        date_str=date_str,
                        drive_file_id=drive_file_id,
                        drive_folder_id=supplier_folder_id,
                        drive_folder_path=folder_path_str,
                        original_filepath=filepath,
                        email_subject=subject,
                        sender=sender,
                        detected_dates=info.get("ambiguous_options", []),
                        amount=info.get("amount"),
                    )
            except Exception as e_dm:
                # Reintentar: el duplicado se detecta en Drive y queda registrado
                log(f"  [ERROR] Fallo al registrar historial: {e_dm}", "ERROR")
                failed_count += 1

        except GeminiAPIError as e:
            log(f"  Error de API Gemini: {e}", "ERROR")
            log(f"  --> Correo NO se marcara como leido (se reintentara la proxima vez).", "WARN")
            failed_count += 1
        except Exception as e:
            log(f"  Error procesando adjunto {os.path.basename(filepath)}: {e}", "ERROR")
            log(f"  --> Correo NO se marcara como leido (se reintentara la proxima vez).", "WARN")
            failed_count += 1
        finally:
            # Limpiar archivo temporal
            try:
                os.remove(filepath)
            except OSError:
                pass

    # 3. Resumen y marcar como leido (solo si ningun documento fallo)
    if failed_count:
        log(f"  Correo UID {uid} NO marcado como leido ({failed_count} documento(s) con error).")
    else:
        if not any_invoice_found:
            log(f"  Ningun adjunto era una factura real en este correo.")
        _finish_email(mail, uid, msg)
    return failed_count


# ── Sincronización del portal B2B de EnvíoMédical ────────────────────────────

def process_enviomedical(drive_service, tmp_dir: str) -> None:
    """
    Descarga las facturas nuevas del portal B2B de EnvíoMédical y las sube
    a Drive con la misma estructura Año / MM-YY / PROVEEDOR.

    - Se consultan las facturas 'desde' la última ejecución menos un margen
      de seguridad (ENVIO_LOOKBACK_DAYS, por defecto 45 días).
    - El estado local (enviomedical_state.json) evita volver a procesar
      facturas ya subidas, aunque el portal siga listándolas.
    - La fecha de carpeta sale de la propia tabla del portal (determinista,
      sin pasar por Gemini).
    - Doble protección contra duplicados: estado local + MD5 en upload_to_drive.
    """
    if not ENVIO_USER or not ENVIO_PASS:
        log("EnvíoMédical: faltan ENVIO_USER / ENVIO_PASS en .env. Sincronización omitida.", "WARN")
        return

    log(f"\n{'#'*60}")
    log("FASE 2: PORTAL B2B ENVÍOMÉDICAL (env.titaniatools.es)")
    log(f"{'#'*60}")

    state = load_state()
    desde = compute_desde_date(state, ENVIO_LOOKBACK_DAYS)
    log(f"Consultando facturas desde: {desde.strftime('%d/%m/%Y')}")

    portal = EnvioMedicalPortal(ENVIO_USER, ENVIO_PASS)
    portal.login()
    log("Login en el portal B2B correcto.")

    invoices = portal.list_invoices(desde)
    log(f"{len(invoices)} factura(s) en el portal desde esa fecha.")

    pending = [inv for inv in invoices if inv["id"] not in state["processed"]]
    if not pending:
        log("No hay facturas nuevas de EnvíoMédical. Todo al día.")
        state["last_run"] = datetime.now().isoformat(timespec="seconds")
        save_state(state)
        return

    log(f"{len(pending)} factura(s) nueva(s) por procesar.")
    supplier_name = sanitize_folder_name(ENVIO_SUPPLIER_NAME)

    ok, fail, dup = 0, 0, 0
    MAX_DRIVE_RETRIES = 3
    for inv in pending:
        log(f"\n  Factura {inv['id']} | {inv['fecha'].strftime('%d/%m/%Y')} | {inv['importe']} €")
        pdf_path = os.path.join(tmp_dir, f"{inv['id']}.pdf")
        for attempt in range(1, MAX_DRIVE_RETRIES + 1):
            try:
                # Estructura Año / MM-YY / PROVEEDOR (idéntica al flujo de Gmail)
                year_folder  = inv["fecha"].strftime("%Y")
                month_folder = inv["fecha"].strftime("%m-%y")
                log(f"  Estructura: {year_folder} / {month_folder} / {supplier_name}")

                year_id     = get_or_create_folder(drive_service, year_folder, DRIVE_ROOT_ID)
                month_id    = get_or_create_folder(drive_service, month_folder, year_id)
                supplier_id = get_or_create_supplier_folder(drive_service, supplier_name, month_id)

                # Anti-duplicado por ID de factura: el portal regenera el PDF en cada
                # descarga (los bytes cambian), así que la comparación MD5 de
                # upload_to_drive nunca coincide. Si el ID de la factura ya aparece
                # en el nombre de un archivo de la carpeta (original o "(1)"),
                # la factura ya está archivada: no se descarga ni se vuelve a subir.
                folder_files = drive_service.files().list(
                    q=f"'{supplier_id}' in parents and trashed = false",
                    spaces="drive",
                    fields="files(id, name)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    corpora="allDrives",
                ).execute().get("files", [])
                ya_esta = next(
                    (f for f in folder_files
                     if f["name"] == f"{inv['id']}.pdf" or f["name"].startswith(f"{inv['id']} (")),
                    None,
                )
                if ya_esta:
                    log(f"  [DUPLICADO] ⏭️ {inv['id']} ya está en Drive como '{ya_esta['name']}'. No se descarga ni se vuelve a subir.", "WARN")
                    try:
                        import data_manager
                        data_manager.add_history_entry(
                            status="SKIPPED_DUPLICATE",
                            filename=f"{inv['id']}.pdf",
                            supplier=supplier_name,
                            date_str=month_folder,
                            drive_folder_id=supplier_id,
                            drive_folder_path=f"{year_folder}/{month_folder}/{supplier_name}",
                            amount=inv.get("importe"),
                            reason=f"Ya existía en Drive como '{ya_esta['name']}'. Descarga y subida omitidas.",
                        )
                    except Exception:
                        pass
                    state["processed"][inv["id"]] = {
                        "fecha": inv["fecha"].strftime("%Y-%m-%d"),
                        "importe": inv["importe"],
                        "subido": datetime.now().isoformat(timespec="seconds"),
                    }
                    save_state(state)
                    dup += 1
                    break

                portal.download_pdf(inv["serie"], inv["docum"], inv["tipodoc"], pdf_path)

                uploaded_id = upload_to_drive(drive_service, pdf_path, supplier_id)
                try:
                    import data_manager
                    if uploaded_id is None:
                        # Duplicado exacto en Drive: se registra con honestidad.
                        log(f"  [DUPLICADO] ⏭️ {inv['id']}.pdf ya estaba en Google Drive. No se ha vuelto a subir.", "WARN")
                        data_manager.add_history_entry(
                            status="SKIPPED_DUPLICATE",
                            filename=f"{inv['id']}.pdf",
                            supplier=supplier_name,
                            date_str=month_folder,
                            drive_file_id=None,
                            drive_folder_id=supplier_id,
                            drive_folder_path=f"{year_folder}/{month_folder}/{supplier_name}",
                            amount=inv.get("importe"),
                            reason="Ya existía en Drive (mismo nombre y contenido MD5). Subida omitida.",
                        )
                    else:
                        data_manager.add_history_entry(
                            status="SUCCESS",
                            filename=f"{inv['id']}.pdf",
                            supplier=supplier_name,
                            date_str=month_folder,
                            drive_file_id=uploaded_id,
                            drive_folder_id=supplier_id,
                            drive_folder_path=f"{year_folder}/{month_folder}/{supplier_name}",
                            amount=inv.get("importe"),
                            reason="Descargado automáticamente del portal B2B",
                        )
                except Exception:
                    pass

                # Registrar en el estado SOLO si todo el proceso terminó bien
                state["processed"][inv["id"]] = {
                    "fecha": inv["fecha"].strftime("%Y-%m-%d"),
                    "importe": inv["importe"],
                    "subido": datetime.now().isoformat(timespec="seconds"),
                }
                save_state(state)
                ok += 1
                break

            except Exception as e:
                es_rate_limit = (
                    "rateLimitExceeded" in str(e)
                    or "userRateLimitExceeded" in str(e)
                    or "rate limit" in str(e).lower()
                )
                if es_rate_limit and attempt < MAX_DRIVE_RETRIES:
                    wait = 30 * attempt
                    log(f"  [DRIVE] Rate limit de Google. Reintento {attempt + 1}/{MAX_DRIVE_RETRIES} en {wait}s...", "WARN")
                    time.sleep(wait)
                else:
                    log(f"  [ERROR] No se pudo procesar la factura {inv['id']}: {e}", "ERROR")
                    fail += 1
                    break
            finally:
                try:
                    os.remove(pdf_path)
                except OSError:
                    pass
        time.sleep(DELAY_BETWEEN_DOWNLOADS)

    state["last_run"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)

    log(f"\nEnvíoMédical completado: {ok} subida(s), {dup} duplicado(s) omitido(s), {fail} error(es).")
    if fail:
        log("Las facturas con error se reintentarán en la próxima ejecución.", "WARN")


def main() -> None:
    """Punto de entrada principal."""
    print()
    print("=" * 62)
    print("           INVOICE MANAGER -- Gestor de Facturas")
    print("=" * 62)
    print()

    # Validar configuración
    missing = []
    if not EMAIL_ADDRESS:
        missing.append("EMAIL_ADDRESS")
    if not APP_PASSWORD:
        missing.append("APP_PASSWORD")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not DRIVE_ROOT_ID:
        missing.append("DRIVE_ROOT_FOLDER_ID")
    if missing:
        log(f"Faltan variables de entorno en .env: {', '.join(missing)}", "ERROR")
        log("  Crea o revisa tu archivo .env (guíate con .env.example)", "ERROR")
        sys.exit(1)

    # Inicializar servicios
    gemini_client = init_gemini()
    drive_service = authenticate_drive()
    mail = connect_gmail()

    # Cargar facturas de referencia para el sistema de rescate
    reference_fps = _load_reference_fingerprints()
    if reference_fps:
        log(f"Facturas de referencia cargadas: {len(reference_fps)} documento(s) en '{REFERENCE_INVOICES_DIR}'")
    else:
        log(f"Sin facturas de referencia (carpeta vacía o no existe). El sistema de rescate estará desactivado.")

    try:
        # ── FASE 1: correos de Gmail ──
        uids = search_invoice_emails(mail)
        if not uids:
            log("No hay correos con facturas pendientes en Gmail.")

        # Crear directorio temporal para adjuntos
        with tempfile.TemporaryDirectory(prefix="invoices_") as tmp_dir:
            for i, uid in enumerate(uids, 1):
                log(f"\n[{i}/{len(uids)}]")
                try:
                    process_invoice(mail, uid, gemini_client, drive_service, tmp_dir, reference_fps=reference_fps)
                except Exception as e:
                    log(f"Error fatal procesando correo UID {uid}: {e}", "ERROR")
                    continue

            # ── FASE 2: facturas del portal B2B de EnvíoMédical ──
            # Se ejecuta siempre, aunque no haya correos pendientes.
            try:
                process_enviomedical(drive_service, tmp_dir)
            except Exception as e:
                log(f"Error en la sincronización del portal EnvíoMédical: {e}", "ERROR")

        print()
        log(f"Proceso completado. {len(uids)} correo(s) procesado(s) + sincronización EnvíoMédical.")

    finally:
        mail.logout()
        log("Desconectado de Gmail.")


if __name__ == "__main__":
    main()
