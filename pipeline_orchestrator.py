"""
pipeline_orchestrator.py — Conector asíncrono y monitor en tiempo real del gestor de facturas.

Permite:
1. Ejecutar el pipeline de procesamiento (Gmail + EnvíoMédical) en un hilo en segundo plano (no bloqueante).
2. Monitorear el estado (idle, running, success, error) y los pasos en tiempo real.
3. Capturar logs detallados de invoice_manager para mostrarlos dinámicamente en el dashboard.
4. Generar un resumen exacto de los documentos procesados en la última ejecución (proveedor, carpeta Drive, estado).
"""

import threading
import tempfile
import traceback
from datetime import datetime
from typing import Callable

import invoice_manager
import data_manager

# ── Estado Global del Orquestador ────────────────────────────────────────────

_STATE = {
    "status": "idle",             # "idle", "running", "success", "error"
    "current_step": "Listo para ejecutar",
    "last_run": None,
    "last_error": None,
    "processed_count": 0,
    "logs": [],
    "session_summary": [],        # Documentos procesados en la sesión más reciente
}
_LOCK = threading.Lock()
_ACTIVE_THREAD = None


def get_pipeline_state() -> dict:
    """Devuelve una copia del estado actual del orquestador."""
    with _LOCK:
        state = _STATE.copy()
        state["stats"] = data_manager.get_stats()
        return state


def _append_log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    with _LOCK:
        _STATE["logs"].append(line)
        if len(_STATE["logs"]) > 250:
            _STATE["logs"] = _STATE["logs"][-250:]
        _STATE["current_step"] = msg


def _execute_pipeline_worker() -> None:
    """Hilo trabajador que ejecuta invoice_manager.py de principio a fin."""
    global _STATE

    # Conectar el callback de logs de invoice_manager
    invoice_manager.set_log_callback(_append_log)

    # Identificar entradas previas para detectar exactamente las nuevas de esta sesión
    initial_ids = {e["id"] for e in data_manager.get_history(limit=500)}

    with _LOCK:
        _STATE["status"] = "running"
        _STATE["last_error"] = None
        _STATE["processed_count"] = 0
        _STATE["session_summary"] = []

    _append_log("Iniciando ciclo de procesamiento...", "INFO")

    mail = None
    try:
        # 1. Validar variables de entorno críticas
        missing = []
        if not invoice_manager.EMAIL_ADDRESS:
            missing.append("EMAIL_ADDRESS")
        if not invoice_manager.APP_PASSWORD:
            missing.append("APP_PASSWORD")
        if not invoice_manager.GEMINI_API_KEY:
            missing.append("GEMINI_API_KEY")
        if not invoice_manager.DRIVE_ROOT_ID:
            missing.append("DRIVE_ROOT_FOLDER_ID")
        if missing:
            raise ValueError(f"Faltan variables en .env: {', '.join(missing)}")

        # 2. Inicializar servicios
        _append_log("Inicializando cliente de Gemini 3.5 Flash-Lite...", "INFO")
        gemini_client = invoice_manager.init_gemini()

        _append_log("Autenticando servicio de Google Drive...", "INFO")
        drive_service = invoice_manager.authenticate_drive()

        _append_log("Conectando a Gmail vía IMAP...", "INFO")
        mail = invoice_manager.connect_gmail()

        # 3. Cargar huellas de facturas de referencia
        reference_fps = invoice_manager._load_reference_fingerprints()

        with tempfile.TemporaryDirectory(prefix="dashboard_invoices_") as tmp_dir:
            # ── FASE 1: Gmail ──
            _append_log("Buscando correos no leídos con facturas...", "INFO")
            uids = invoice_manager.search_invoice_emails(mail)
            _append_log(f"Se encontraron {len(uids)} correo(s) no leídos para procesar.", "INFO")

            for i, uid in enumerate(uids, 1):
                _append_log(f"Procesando correo {i} de {len(uids)}...", "INFO")
                try:
                    invoice_manager.process_invoice(
                        mail, uid, gemini_client, drive_service, tmp_dir, reference_fps=reference_fps
                    )
                except Exception as e:
                    _append_log(f"Error procesando correo UID {uid}: {e}", "ERROR")

            with _LOCK:
                _STATE["processed_count"] += len(uids)

            # ── FASE 2: Portal B2B EnvíoMédical ──
            _append_log("Iniciando sincronización B2B EnvíoMédical...", "INFO")
            try:
                invoice_manager.process_enviomedical(drive_service, tmp_dir)
            except Exception as e:
                _append_log(f"Error en EnvíoMédical: {e}", "WARN")

        # 4. Calcular resumen de la sesión
        current_entries = data_manager.get_history(limit=500)
        new_entries = [e for e in current_entries if e["id"] not in initial_ids]

        ok_count = sum(1 for e in new_entries if e.get("status") == "SUCCESS")
        amb_count = sum(1 for e in new_entries if e.get("status") == "AMBIGUOUS_DATE")
        disc_count = sum(1 for e in new_entries if e.get("status") == "DISCARDED")

        summary_line = f"Fin de ciclo: {ok_count} facturas archivadas en Drive, {amb_count} dudosas, {disc_count} descartadas."
        _append_log(summary_line, "SUCCESS" if ok_count > 0 else "INFO")

        with _LOCK:
            _STATE["status"] = "success"
            _STATE["current_step"] = "Completado con éxito"
            _STATE["last_run"] = datetime.now().isoformat(timespec="seconds")
            _STATE["session_summary"] = new_entries

    except Exception as e:
        err_msg = str(e)
        trace = traceback.format_exc()
        _append_log(f"Error fatal en la ejecución: {err_msg}", "ERROR")
        with _LOCK:
            _STATE["status"] = "error"
            _STATE["last_error"] = err_msg
            _STATE["current_step"] = f"Error: {err_msg}"
            _STATE["last_run"] = datetime.now().isoformat(timespec="seconds")
    finally:
        if mail:
            try:
                mail.logout()
                _append_log("Desconectado de Gmail correctamente.", "INFO")
            except Exception:
                pass


def trigger_pipeline_run() -> bool:
    """Dispara una nueva ejecución en segundo plano si no hay otra en marcha."""
    global _ACTIVE_THREAD
    with _LOCK:
        if _STATE["status"] == "running":
            return False  # Ya hay una ejecución activa

    _ACTIVE_THREAD = threading.Thread(target=_execute_pipeline_worker, daemon=True)
    _ACTIVE_THREAD.start()
    return True
