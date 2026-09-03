#!/usr/bin/env python3
"""
EnvíoMédical Portal — Cliente de descarga de facturas del portal B2B.

Se conecta a env.titaniatools.es (plataforma B2B de EnvíoMédical / Titania Tools),
consulta la lista de facturas desde una fecha y descarga los PDF.

Flujo técnico (verificado empíricamente):
1. POST /tools/main.php con username, passwd y accion=main_valida_login
   -> devuelve cookie de sesion ('nombredesesion') y el token CSRF en el HTML.
2. GET  /tools/Informes.php?csrf=...&accion=facturas
   -> pagina con el formulario de filtrado (campo 'combo_cliente').
3. POST /tools/Informes.php con desde_fecha (YYYY-MM-DD), combo_cliente,
   accion=facturas, csrf y bot_accion="Lista de Facturas"
   -> tabla HTML con las facturas (cliente, fecha dd/mm/yy, numero FV01|NNNNNN,
      importe y formulario oculto de descarga).
4. POST /tools/lanzareport.php con serie, docum, tipodoc y csrf
   -> bytes del PDF (application/pdf).

El estado de sincronizacion (facturas ya procesadas y fecha de ultima
ejecucion) se guarda en 'enviomedical_state.json' junto a este archivo.
"""

import os
import re
import json
import time
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

# ── Configuración ────────────────────────────────────────────────────────────

BASE_URL = os.getenv("ENVIO_PORTAL_URL", "https://env.titaniatools.es")
STATE_FILENAME = "enviomedical_state.json"
REQUEST_TIMEOUT = 60          # segundos por peticion HTTP
DELAY_BETWEEN_DOWNLOADS = 1   # segundos de cortesia entre descargas de PDF


class EnvioMedicalError(Exception):
    """Error del portal B2B (login, sesion expirada, respuesta inesperada)."""
    pass


# ── Estado local (facturas ya procesadas) ────────────────────────────────────

def state_path() -> str:
    """Ruta del archivo de estado, junto a este módulo."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), STATE_FILENAME)


def load_state() -> dict:
    """
    Carga el estado de sincronización desde disco.
    Estructura:
      {
        "processed": { "FV01-2636424": {"fecha": "2026-08-31", "subido": "..."}, ... },
        "last_run": "2026-09-02T22:00:00" | null
      }
    """
    path = state_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("processed"), dict):
                data.setdefault("last_run", None)
                return data
        except (json.JSONDecodeError, OSError):
            pass  # Estado corrupto -> empezar de cero (Drive deduplica por MD5)
    return {"processed": {}, "last_run": None}


def save_state(state: dict) -> None:
    with open(state_path(), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def compute_desde_date(state: dict, lookback_days: int) -> datetime:
    """
    Calcula la fecha 'desde' para consultar el portal:
    - Si hay ejecución previa: última ejecución menos el margen de seguridad.
    - Primera ejecución: hoy menos 'lookback_days' (p. ej. 45 días).
    """
    last = state.get("last_run")
    if last:
        try:
            base = datetime.fromisoformat(last)
            return base - timedelta(days=lookback_days)
        except ValueError:
            pass
    return datetime.now() - timedelta(days=lookback_days)


# ── Cliente del portal ───────────────────────────────────────────────────────

class EnvioMedicalPortal:
    """Sesión autenticada contra el portal B2B de EnvíoMédical."""

    def __init__(self, user: str, password: str):
        self.user = user
        self.password = password
        self.csrf: str | None = None
        self.client_code: str | None = None
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) InvoiceManager/2.0",
            "Referer": f"{BASE_URL}/tools/main.php",
        })

    # -- HTTP con reintento simple --

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        last_exc = None
        for attempt in (1, 2):
            try:
                return self.s.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            except requests.RequestException as e:
                last_exc = e
                time.sleep(2 * attempt)
        raise EnvioMedicalError(f"Error de red tras 2 intentos: {last_exc}")

    # -- Autenticación --

    def login(self) -> None:
        """Inicia sesión y guarda el token CSRF de la sesión."""
        r = self._request("POST", f"{BASE_URL}/tools/main.php", data={
            "username": self.user,
            "passwd": self.password,
            "accion": "main_valida_login",
            "dispositivo_width": "1920",
        })

        if "Introduzca sus credenciales" in r.text:
            raise EnvioMedicalError("Login rechazado por el portal (credenciales incorrectas o expiradas).")

        m = re.search(r"csrf=([0-9a-f]{16,64})", r.text)
        if not m:
            raise EnvioMedicalError("No se encontró el token CSRF tras el login. ¿Cambió el portal?")
        self.csrf = m.group(1)

    def _ensure_session(self) -> None:
        """Re-loguea automáticamente si la sesión caducó a mitad de proceso."""
        if self.csrf is None:
            self.login()

    # -- Listado de facturas --

    def list_invoices(self, desde: datetime) -> list[dict]:
        """
        Devuelve las facturas del portal emitidas desde la fecha indicada.
        Cada elemento: {id, serie, docum, tipodoc, fecha(datetime), importe, cliente}
        """
        self._ensure_session()

        # Página base del informe (necesaria para obtener combo_cliente)
        r0 = self._request("GET", f"{BASE_URL}/tools/Informes.php",
                           params={"csrf": self.csrf, "accion": "facturas"})
        soup0 = BeautifulSoup(r0.text, "html.parser")
        combo = soup0.find("input", {"name": "combo_cliente"})
        self.client_code = combo.get("value") if combo else None

        # Consulta filtrada por fecha
        r = self._request("POST", f"{BASE_URL}/tools/Informes.php", data={
            "desde_fecha": desde.strftime("%Y-%m-%d"),
            "combo_cliente": self.client_code or "",
            "accion": "facturas",
            "csrf": self.csrf,
            "caja_valor": "",
            "bot_accion": "Lista de Facturas ",
        })

        soup = BeautifulSoup(r.text, "html.parser")
        tbody = soup.find("tbody", id="tbodyTablaDetalle_excel")
        if tbody is None:
            if "Introduzca sus credenciales" in r.text:
                # Sesión expirada entre peticiones -> reintentar una vez con sesión nueva
                self.login()
                return self.list_invoices(desde)
            raise EnvioMedicalError("No se encontró la tabla de facturas en la respuesta del portal.")

        invoices = []
        for tr in tbody.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue

            # Formulario oculto de descarga del PDF (serie / docum / tipodoc)
            form = tr.find("form")
            if form is None:
                continue
            fdata = {inp.get("name"): inp.get("value", "") for inp in form.find_all("input")}
            serie = fdata.get("serie", "")
            docum = fdata.get("docum", "")
            tipodoc = fdata.get("tipodoc", "FAC")
            if not serie or not docum:
                continue

            # Fecha en formato dd/mm/yy (a veces dd/mm/yyyy)
            raw_fecha = tds[1].get_text(strip=True)
            fecha = None
            for fmt in ("%d/%m/%y", "%d/%m/%Y"):
                try:
                    fecha = datetime.strptime(raw_fecha, fmt)
                    break
                except ValueError:
                    continue
            if fecha is None:
                continue  # Fila sin fecha válida: no se puede clasificar

            invoices.append({
                "id": f"{serie}-{docum}",
                "serie": serie,
                "docum": docum,
                "tipodoc": tipodoc,
                "fecha": fecha,
                "importe": tds[3].get_text(strip=True),
                "cliente": tds[0].get_text(strip=True),
            })

        return invoices

    # -- Descarga de PDF --

    def download_pdf(self, serie: str, docum: str, tipodoc: str, dest_path: str) -> str:
        """
        Descarga el PDF de una factura y lo guarda en dest_path.
        Devuelve la ruta escrita. Lanza EnvioMedicalError si la respuesta
        no es un PDF válido.
        """
        self._ensure_session()
        r = self._request("POST", f"{BASE_URL}/tools/lanzareport.php", data={
            "serie": serie,
            "docum": docum,
            "tipodoc": tipodoc,
            "csrf": self.csrf,
        })

        # El portal antepone whitespace antes de la cabecera %PDF: se limpia
        content = r.content.lstrip()
        if not content.startswith(b"%PDF"):
            raise EnvioMedicalError(
                f"La respuesta para {serie}-{docum} no es un PDF (status {r.status_code})."
            )

        with open(dest_path, "wb") as f:
            f.write(content)
        return dest_path
