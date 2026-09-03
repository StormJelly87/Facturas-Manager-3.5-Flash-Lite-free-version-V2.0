"""
dashboard/app.py — Servidor Web FastAPI para la interfaz de control del Gestor de Facturas.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import data_manager
import pipeline_orchestrator
import scheduler_service
import invoice_manager

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Iniciar programador en segundo plano al arrancar
    scheduler_service.start_scheduler()
    yield
    # Detener al apagar
    scheduler_service.stop_scheduler()


app = FastAPI(title="Invoice Manager Dashboard", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Frontend HTML ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Error: No se encontró dashboard/static/index.html</h1>"


# ── Endpoints de Estado y Control ───────────────────────────────────────────

@app.get("/api/status")
def get_status():
    """Devuelve el estado en tiempo real del pipeline, estadísticas y últimos logs."""
    state = pipeline_orchestrator.get_pipeline_state()
    state["scheduler_running"] = scheduler_service.is_scheduler_running()
    return state


@app.post("/api/run")
def trigger_run():
    """Inicia la ejecución manual del proceso de facturas en segundo plano."""
    started = pipeline_orchestrator.trigger_pipeline_run()
    if not started:
        return JSONResponse(
            status_code=409,
            content={"success": False, "message": "Ya hay una ejecución en marcha. Espera a que termine."},
        )
    return {"success": True, "message": "Proceso de facturas iniciado correctamente."}


# ── Endpoints de Historial y Bandejas ────────────────────────────────────────

@app.get("/api/history")
def get_successful_history(limit: int = 100):
    return data_manager.get_history(status="SUCCESS", limit=limit)


@app.get("/api/discarded")
def get_discarded_invoices(limit: int = 100):
    return data_manager.get_history(status="DISCARDED", limit=limit)


@app.get("/api/ambiguous")
def get_ambiguous_invoices(limit: int = 100):
    return data_manager.get_history(status="AMBIGUOUS_DATE", limit=limit)


@app.get("/api/preview/{item_id}")
def preview_file(item_id: str):
    """Sirve el documento en cuarentena para previsualización en la interfaz."""
    filepath = data_manager.get_quarantine_path(item_id)
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="Archivo no disponible para previsualizar")

    ext = Path(filepath).suffix.lower()
    media_types = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".tiff": "image/tiff",
        ".webp": "image/webp",
    }
    media_type = media_types.get(ext, "application/octet-stream")
    return FileResponse(filepath, media_type=media_type)


# ── Acciones de Rescate y Resolución de Ambigüedad ───────────────────────────

class RescueRequest(BaseModel):
    supplier: str
    date: str  # YYYY-MM-DD o MM-YY
    always_accept: bool = True
    reason_notes: Optional[str] = ""


@app.post("/api/rescue/{item_id}")
def rescue_invoice(item_id: str, req: RescueRequest):
    """Rescata un documento descartado, lo sube a Drive y memoriza la regla."""
    entry = data_manager.get_history_entry(item_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Registro no encontrado")

    filepath = data_manager.get_quarantine_path(item_id)
    if not filepath or not os.path.exists(filepath):
        raise HTTPException(status_code=400, detail="El archivo en cuarentena ya no existe en disco")

    supplier_name = invoice_manager.sanitize_folder_name(req.supplier)
    
    # Parsear fecha
    try:
        if "-" in req.date and len(req.date.split("-")[0]) == 4:
            dt = datetime.strptime(req.date, "%Y-%m-%d")
            year_folder = str(dt.year)
            month_folder = dt.strftime("%m-%y")
        else:
            parts = req.date.split("-")
            month_folder = req.date
            year_folder = f"20{parts[1]}" if len(parts) == 2 else datetime.now().strftime("%Y")
    except Exception:
        year_folder = datetime.now().strftime("%Y")
        month_folder = datetime.now().strftime("%m-%y")

    # Subir a Google Drive
    try:
        drive_service = invoice_manager.authenticate_drive()
        year_id = invoice_manager.get_or_create_folder(drive_service, year_folder, invoice_manager.DRIVE_ROOT_ID)
        month_id = invoice_manager.get_or_create_folder(drive_service, month_folder, year_id)
        supplier_id = invoice_manager.get_or_create_supplier_folder(drive_service, supplier_name, month_id)
        uploaded_id = invoice_manager.upload_to_drive(drive_service, filepath, supplier_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al subir a Google Drive: {e}")

    # Memorizar regla aprendida en vendor_rules.json con motivo detallado
    if req.always_accept:
        data_manager.save_vendor_rule(
            supplier_name,
            always_accept=True,
            trusted_senders=[entry.get("sender")] if entry.get("sender") else None,
            notes=req.reason_notes or "Rescatado manualmente desde el dashboard",
            learned_from="rescate_manual",
            origin_document=entry.get("filename"),
            user_reason=req.reason_notes or "",
        )

    # Actualizar historial a éxito
    data_manager.update_history_entry(item_id, {
        "status": "SUCCESS",
        "supplier": supplier_name,
        "date_str": month_folder,
        "drive_file_id": uploaded_id,
        "drive_folder_id": supplier_id,
        "drive_folder_path": f"{year_folder} / {month_folder} / {supplier_name}",
        "reason": f"Rescatada manualmente por el usuario. {req.reason_notes}".strip(),
    })

    return {
        "success": True,
        "message": f"Factura rescatada con éxito y archivada en {year_folder}/{month_folder}/{supplier_name}",
        "supplier": supplier_name,
        "folder": f"{year_folder}/{month_folder}/{supplier_name}",
    }


class AmbiguityResolutionRequest(BaseModel):
    day: int
    month: int
    year: int
    format: str  # "DD/MM/YYYY" o "MM/DD/YYYY"


@app.post("/api/resolve_ambiguity/{item_id}")
def resolve_ambiguity(item_id: str, req: AmbiguityResolutionRequest):
    """
    Resuelve una fecha ambigua confirmada por el usuario:
    1. Reubica el archivo en Drive a la carpeta correcta sin duplicarlo.
    2. Memoriza la regla de formato para siempre en vendor_rules.json.
    """
    entry = data_manager.get_history_entry(item_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Registro no encontrado")

    supplier_name = invoice_manager.sanitize_folder_name(entry.get("supplier", "DESCONOCIDO"))
    year_folder = str(req.year)
    month_folder = f"{req.month:02d}-{str(req.year)[-2:]}"

    try:
        drive_service = invoice_manager.authenticate_drive()
        year_id = invoice_manager.get_or_create_folder(drive_service, year_folder, invoice_manager.DRIVE_ROOT_ID)
        month_id = invoice_manager.get_or_create_folder(drive_service, month_folder, year_id)
        new_supplier_id = invoice_manager.get_or_create_supplier_folder(drive_service, supplier_name, month_id)

        drive_file_id = entry.get("drive_file_id")
        old_folder_id = entry.get("drive_folder_id")

        if drive_file_id and old_folder_id:
            if new_supplier_id != old_folder_id:
                # Mover el archivo entre carpetas en Drive sin duplicar
                invoice_manager.move_drive_file(drive_service, drive_file_id, old_folder_id, new_supplier_id)
        else:
            # Si aún no estaba en Drive, subir desde cuarentena
            filepath = data_manager.get_quarantine_path(item_id)
            if filepath and os.path.exists(filepath):
                drive_file_id = invoice_manager.upload_to_drive(drive_service, filepath, new_supplier_id)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en Google Drive: {e}")

    # Guardar regla en vendor_rules.json con origen y explicación detallada
    data_manager.save_vendor_rule(
        supplier_name,
        date_format=req.format,
        learned_from="resolucion_ambiguedad",
        origin_document=entry.get("filename"),
        notes=f"Formato confirmado: día={req.day}, mes={req.month}, año={req.year}",
    )

    # Actualizar historial
    data_manager.update_history_entry(item_id, {
        "status": "SUCCESS",
        "date_str": month_folder,
        "drive_folder_id": new_supplier_id,
        "drive_folder_path": f"{year_folder} / {month_folder} / {supplier_name}",
        "reason": f"Fecha confirmada ({req.day:02d}/{req.month:02d}/{req.year}) - Formato {req.format}",
    })

    return {
        "success": True,
        "message": f"Regla memorizada ({req.format}) y factura archivada en {year_folder}/{month_folder}/{supplier_name}",
    }


@app.post("/api/discard_ambiguous/{item_id}")
def discard_ambiguous(item_id: str):
    """Descarta un documento en dudosas sin validarlo como factura."""
    entry = data_manager.get_history_entry(item_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Registro no encontrado")
    
    success = data_manager.discard_history_entry(
        item_id, reason="Descartado por el usuario desde Facturas Dudosas (no se considera factura válida)"
    )
    return {"success": True, "message": "Documento descartado correctamente"}


# ── Endpoints de Reglas Memorizadas y Programación ───────────────────────────

@app.get("/api/rules")
def get_rules():
    return data_manager.load_vendor_rules()


@app.delete("/api/rules/{supplier_name}")
def delete_rule(supplier_name: str):
    success = data_manager.delete_vendor_rule(supplier_name)
    if not success:
        raise HTTPException(status_code=404, detail="Regla no encontrada")
    return {"success": True, "message": "Regla eliminada correctamente"}


class ScheduleRequest(BaseModel):
    enabled: bool
    frequency: str = "weekly"
    day_of_week: int = 0
    day_of_month: int = 1
    hour: int = 9
    minute: int = 0


@app.get("/api/schedule")
def get_schedule():
    return data_manager.load_schedule_config()


@app.post("/api/schedule")
def update_schedule(req: ScheduleRequest):
    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    saved = data_manager.save_schedule_config(payload)
    return {"success": True, "schedule": saved}
