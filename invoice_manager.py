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
from datetime import datetime
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

# ── Utilidades ───────────────────────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    """Imprime un mensaje con timestamp (seguro para consolas Windows)."""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        # Fallback: eliminar caracteres no soportados por la consola
        print(line.encode("ascii", errors="replace").decode("ascii"))


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


def search_invoice_emails(mail: imaplib.IMAP4_SSL) -> list[str]:
    """
    Busca correos no leídos que contengan 'factura', 'facturas' o 'invoice'.
    Devuelve una lista de UIDs.
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

    uids = data[0].split()
    log(f"{len(uids)} correo(s) encontrado(s).")
    return [uid.decode() for uid in uids]


def fetch_email(mail: imaplib.IMAP4_SSL, uid: str) -> email.message.Message:
    """Descarga un mensaje completo por UID."""
    status, data = mail.uid("FETCH", uid, "(RFC822)")
    if status != "OK":
        raise RuntimeError(f"No se pudo descargar el correo UID {uid}")
    raw = data[0][1]
    return email.message_from_bytes(raw)


def extract_attachments(msg: email.message.Message, tmp_dir: str) -> list[str]:
    """
    Extrae los adjuntos válidos (PDF, imágenes) del mensaje.
    Devuelve una lista de rutas a los archivos extraídos.
    """
    saved = []
    for part in msg.walk():
        content_disposition = str(part.get("Content-Disposition", ""))
        if "attachment" not in content_disposition:
            continue

        filename = decode_mime_header(part.get_filename())
        if not filename:
            continue

        ext = Path(filename).suffix.lower()
        if ext not in INVOICE_EXTENSIONS:
            log(f"  Adjunto ignorado (extension {ext}): {filename}")
            continue

        filepath = os.path.join(tmp_dir, filename)
        # Evitar colisiones de nombre
        counter = 1
        base, extension = os.path.splitext(filepath)
        while os.path.exists(filepath):
            filepath = f"{base}_{counter}{extension}"
            counter += 1

        with open(filepath, "wb") as f:
            f.write(part.get_payload(decode=True))

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


def validate_is_invoice(model: genai.GenerativeModel, filepath: str, sender_email: str = "", reference_fps: list | None = None) -> dict | None:
    """
    Validación con jerarquía: Origen Email > Validación Fiscal > Contenido PDF.

    Envía el adjunto a Gemini para confirmar si es una factura real.
    - Si NO es factura -> devuelve None (el archivo se ignora).
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
    pdf_text_extracted = False
    pdf_text_lower = ""
    
    if filepath.lower().endswith(".pdf"):
        try:
            with pdfplumber.open(filepath) as pdf:
                pdf_text_lower = " ".join(page.extract_text() or "" for page in pdf.pages).lower()

            pdf_text_extracted = bool(pdf_text_lower.strip())

            # 1. Regex Estricto (ej. 06/03/2026, 6-3-2026)
            match = re.search(r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b', pdf_text_lower)
            if match:
                d = int(match.group(1))
                m = int(match.group(2))
                y = int(match.group(3))
                if 1 <= d <= 31 and 1 <= m <= 12:
                    forced_date = datetime(y, m, d)
            
            # 2. Mapeo Matemático de Meses (completos + abreviados, ES + EN)
            if not forced_date:
                all_month_names = {
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
                    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
                    "jun": 6, "jul": 7, "aug": 8,
                    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
                }

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
                        log(f"  [FECHA] Formato dd-Mon-yyyy detectado: {forced_day_num:02d}-{forced_month_num:02d}-{y}")

                # 2b. Formato "6 de marzo" (español con preposición)
                if not forced_date and forced_month_num is None:
                    month_match = re.search(
                        r'\b(\d{1,2})\s+(?:de\s+)?(' + abbrev_month_pattern + r')\b',
                        pdf_text_lower
                    )
                    if month_match:
                        forced_day_num = int(month_match.group(1))
                        forced_month_num = all_month_names[month_match.group(2)]
                    else:
                        for m_name, m_num in all_month_names.items():
                            if re.search(rf'\b{m_name}\b', pdf_text_lower):
                                forced_month_num = m_num
                                break
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
  "date": "Fecha de la factura en formato YYYY-MM-DD (ejemplo: 2026-02-10 para el 10 de febrero de 2026)"
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
        is_invoice_str = result.get("is_invoice", "NO").strip().upper()
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
                return None

        # -- Extraer datos crudos de Gemini --
        supplier = result.get("supplier", "DESCONOCIDO").strip()
        tax_id   = result.get("tax_id", "DESCONOCIDO").strip()
        raw_date = result.get("date", "").strip()

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
                if tax_id_valid:
                    log(f"  [FALLBACK] CIF/VAT rescatado del PDF: {tax_id}")

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
                all_months = [
                    # Español completo
                    "enero", "febrero", "marzo", "abril", "mayo", "junio",
                    "julio", "agosto", "septiembre", "setiembre", "octubre",
                    "noviembre", "diciembre",
                    # Español abreviado
                    "ene", "feb", "mar", "abr", "jun",
                    "jul", "ago", "sep", "oct", "nov", "dic",
                    # Inglés completo
                    "january", "february", "march", "april", "may", "june",
                    "july", "august", "september", "october", "november", "december",
                    # Inglés abreviado
                    "jan", "feb", "mar", "apr", "jun",
                    "jul", "aug", "sep", "oct", "nov", "dec",
                ]
                for month_name in all_months:
                    if re.search(rf'\b{month_name}\b', pdf_text_lower):
                        has_any_date = True
                        break

            if not has_any_date:
                # Último recurso: comparar con facturas de referencia
                if reference_fps and _matches_reference_invoice(filepath, reference_fps):
                    log(f"  [RESCATE] Sin fecha verificable, pero el documento coincide con una factura de referencia. Continuando...")
                else:
                    log(f"  [FILTRO-FECHA] Sin fecha verificable en '{filename}' -> Descartado (posible ticket/recibo)")
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
        try:
            if raw_date:
                # Intentar formato ISO estricto primero (el solicitado a la IA)
                try:
                    dt = datetime.strptime(raw_date, "%Y-%m-%d")
                except ValueError:
                    dt = dateutil_parser.parse(raw_date, dayfirst=True)
            else:
                dt = datetime.now()
        except (ValueError, OverflowError):
            log(f"  Fecha no parseable: '{raw_date}', usando fecha actual.", "WARN")
            dt = datetime.now()

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

        # Filtro de Seguridad Automático (Fecha Futura)
        now = datetime.now()
        if dt > now:
            log(f"  [SEGURIDAD] Fecha futura detectada ({dt.strftime('%Y-%m-%d')}). Invirtiendo día y mes automáticamente.")
            try:
                dt = dt.replace(month=dt.day, day=dt.month)
            except ValueError:
                pass
            
            if dt > now:
                dt = now

        date_str = dt.strftime("%m-%y")

        # Doble comprobación: log del día, mes y carpeta destino
        log(f"  [FECHA] Procesando dia: {dt.strftime('%d')}, mes: {dt.strftime('%m')}. Carpeta destino: {date_str}")

        log(f"  [SI] Factura confirmada -- Proveedor: {supplier} | CIF: {tax_id} | Fecha: {date_str}")
        return {"supplier": supplier, "date": date_str}

    except (json.JSONDecodeError, KeyError) as e:
        log(f"  Error al parsear respuesta de Gemini: {e}", "ERROR")
        log(f"    Respuesta raw: {response.text[:200] if response else 'N/A'}", "DEBUG")
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


# ── Flujo principal ──────────────────────────────────────────────────────────

def process_invoice(
    mail: imaplib.IMAP4_SSL,
    uid: str,
    gemini_client: genai.GenerativeModel,
    drive_service,
    tmp_dir: str,
    reference_fps: list | None = None,
) -> None:
    """Procesa un unico correo: extraer adjuntos -> validar -> subir -> marcar leido."""

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
        mark_as_read(mail, uid)
        return

    # 1. Extraer adjuntos
    attachments = extract_attachments(msg, tmp_dir)
    if not attachments:
        log("  No se encontraron adjuntos validos, marcando como leido igualmente.")
        mark_as_read(mail, uid)
        return

    # 2. Procesar cada adjunto
    any_invoice_found = False
    had_api_error = False
    for filepath in attachments:
        try:
            # 2a. Validar con Gemini (anti-falsos positivos)
            info = validate_is_invoice(gemini_client, filepath, sender_email=sender, reference_fps=reference_fps)
            if info is None:
                log(f"  Adjunto no es factura, saltando...")
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
            upload_to_drive(drive_service, filepath, supplier_folder_id)

        except GeminiAPIError as e:
            log(f"  Error de API Gemini: {e}", "ERROR")
            log(f"  --> Correo NO se marcara como leido (se reintentara la proxima vez).", "WARN")
            had_api_error = True
        except Exception as e:
            log(f"  Error procesando adjunto {os.path.basename(filepath)}: {e}", "ERROR")
        finally:
            # Limpiar archivo temporal
            try:
                os.remove(filepath)
            except OSError:
                pass

    # 3. Resumen y marcar como leido (solo si no hubo errores de API)
    if had_api_error:
        log(f"  Correo UID {uid} NO marcado como leido (hubo errores de API).")
    else:
        if not any_invoice_found:
            log(f"  Ningun adjunto era una factura real en este correo.")
        mark_as_read(mail, uid)


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

    ok, fail = 0, 0
    MAX_DRIVE_RETRIES = 3
    for inv in pending:
        log(f"\n  Factura {inv['id']} | {inv['fecha'].strftime('%d/%m/%Y')} | {inv['importe']} €")
        pdf_path = os.path.join(tmp_dir, f"{inv['id']}.pdf")
        for attempt in range(1, MAX_DRIVE_RETRIES + 1):
            try:
                portal.download_pdf(inv["serie"], inv["docum"], inv["tipodoc"], pdf_path)

                # Estructura Año / MM-YY / PROVEEDOR (idéntica al flujo de Gmail)
                year_folder  = inv["fecha"].strftime("%Y")
                month_folder = inv["fecha"].strftime("%m-%y")
                log(f"  Estructura: {year_folder} / {month_folder} / {supplier_name}")

                year_id     = get_or_create_folder(drive_service, year_folder, DRIVE_ROOT_ID)
                month_id    = get_or_create_folder(drive_service, month_folder, year_id)
                supplier_id = get_or_create_supplier_folder(drive_service, supplier_name, month_id)

                upload_to_drive(drive_service, pdf_path, supplier_id)

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

    log(f"\nEnvíoMédical completado: {ok} subida(s), {fail} error(es).")
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
