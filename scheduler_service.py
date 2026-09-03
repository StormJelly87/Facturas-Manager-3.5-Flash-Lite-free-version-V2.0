"""
scheduler_service.py — Programador desatendido en segundo plano con recuperación de ejecuciones pendientes (Catch-Up).

Vigila la configuración en data/schedule_config.json y dispara ejecuciones
automáticas (semanales, mensuales o diarias).

Si el ordenador estuvo apagado a la hora prevista, al iniciar el sistema detecta
que la ejecución quedó pendiente y la lanza automáticamente.
"""

import time
import threading
from datetime import datetime, timedelta

import data_manager
import pipeline_orchestrator

_SCHEDULER_THREAD = None
_RUNNING = False
_LOCK = threading.Lock()


def _check_and_trigger_catchup(config: dict, now: datetime) -> bool:
    """
    Comprueba si tocaba ejecutar mientras el ordenador estuvo apagado
    (Catch-Up / Recuperación de ejecuciones perdidas).
    """
    if not config.get("enabled"):
        return False

    freq = config.get("frequency", "weekly")
    target_hour = int(config.get("hour", 9))
    target_min = int(config.get("minute", 0))
    last_run_str = config.get("last_run")

    last_run_dt = None
    if last_run_str:
        try:
            last_run_dt = datetime.fromisoformat(last_run_str)
        except Exception:
            pass

    # Determinar si hoy era el día programado y la hora ya pasó
    missed = False

    if freq == "daily":
        scheduled_today = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
        if now > scheduled_today:
            # Si no se ejecutó hoy después de la hora fijada
            if not last_run_dt or last_run_dt < scheduled_today:
                missed = True

    elif freq == "weekly":
        target_dow = int(config.get("day_of_week", 0))  # 0 = Lunes
        if now.weekday() == target_dow:
            scheduled_today = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
            if now > scheduled_today:
                if not last_run_dt or last_run_dt < scheduled_today:
                    missed = True

    elif freq == "monthly":
        target_dom = int(config.get("day_of_month", 1))
        if now.day == target_dom:
            scheduled_today = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
            if now > scheduled_today:
                if not last_run_dt or last_run_dt < scheduled_today:
                    missed = True

    if missed:
        print(f"[SCHEDULER] ⏰ Ejecución pendiente detectada ({freq}). El equipo estaba apagado a las {target_hour:02d}:{target_min:02d}. Iniciando ahora...")
        started = pipeline_orchestrator.trigger_pipeline_run()
        if started:
            config["last_run"] = now.isoformat(timespec="seconds")
            data_manager.save_schedule_config(config)
            return True

    return False


def _scheduler_loop() -> None:
    """Bucle del programador que verifica cada 30 segundos si toca ejecutar o si hubo ejecuciones perdidas."""
    global _RUNNING
    last_triggered_key = None
    first_check_done = False

    while True:
        with _LOCK:
            if not _RUNNING:
                break

        try:
            config = data_manager.load_schedule_config()
            now = datetime.now()

            # En el primer inicio tras arrancar el equipo, verificar si quedó alguna pendiente
            if not first_check_done:
                first_check_done = True
                _check_and_trigger_catchup(config, now)

            if config.get("enabled"):
                target_hour = int(config.get("hour", 9))
                target_min = int(config.get("minute", 0))
                freq = config.get("frequency", "weekly")

                is_time = (now.hour == target_hour and now.minute == target_min)
                should_run = False
                trigger_key = None

                if is_time:
                    if freq == "daily":
                        trigger_key = f"daily_{now.strftime('%Y%m%d_%H%M')}"
                        should_run = True
                    elif freq == "weekly":
                        target_dow = int(config.get("day_of_week", 0))
                        if now.weekday() == target_dow:
                            trigger_key = f"weekly_{now.strftime('%Y%W_%H%M')}"
                            should_run = True
                    elif freq == "monthly":
                        target_dom = int(config.get("day_of_month", 1))
                        if now.day == target_dom:
                            trigger_key = f"monthly_{now.strftime('%Y%m_%H%M')}"
                            should_run = True

                # Disparar solo una vez en ese minuto exacto
                if should_run and trigger_key != last_triggered_key:
                    last_triggered_key = trigger_key
                    print(f"[SCHEDULER] Disparando ejecución automática programada ({freq})...")
                    started = pipeline_orchestrator.trigger_pipeline_run()
                    if started:
                        config["last_run"] = now.isoformat(timespec="seconds")
                        data_manager.save_schedule_config(config)

        except Exception as e:
            print(f"[SCHEDULER-ERROR] Error en el bucle del programador: {e}")

        time.sleep(30)


def get_schedule_info() -> dict:
    """Devuelve la configuración y un resumen textual entendible de la programación activa."""
    config = data_manager.load_schedule_config()
    enabled = bool(config.get("enabled"))
    freq = config.get("frequency", "weekly")
    h = int(config.get("hour", 9))
    m = int(config.get("minute", 0))
    dow = int(config.get("day_of_week", 0))
    dom = int(config.get("day_of_month", 1))
    last_run = config.get("last_run")

    dias_semana = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

    if freq == "weekly":
        freq_text = f"Semanal: Cada {dias_semana[dow]} a las {h:02d}:{m:02d}"
    elif freq == "monthly":
        freq_text = f"Mensual: El día {dom} de cada mes a las {h:02d}:{m:02d}"
    else:
        freq_text = f"Diaria: Todos los días a las {h:02d}:{m:02d}"

    last_run_text = datetime.fromisoformat(last_run).strftime("%d/%m/%Y a las %H:%M") if last_run else "Ninguna registrada todavía"

    return {
        "enabled": enabled,
        "frequency": freq,
        "frequency_text": freq_text,
        "day_of_week": dow,
        "day_of_month": dom,
        "hour": h,
        "minute": m,
        "last_run": last_run,
        "last_run_text": last_run_text,
        "catchup_enabled": True,
        "catchup_text": "Recuperación automática activa: si el ordenador estaba apagado en el horario fijado, se ejecutará en cuanto abras la aplicación.",
    }


def start_scheduler() -> None:
    """Inicia el servicio de programación en segundo plano."""
    global _SCHEDULER_THREAD, _RUNNING
    with _LOCK:
        if _RUNNING:
            return
        _RUNNING = True
        _SCHEDULER_THREAD = threading.Thread(target=_scheduler_loop, daemon=True)
        _SCHEDULER_THREAD.start()
        print("[SCHEDULER] Servicio de programación iniciado en segundo plano (con soporte Catch-Up).")


def stop_scheduler() -> None:
    """Detiene el servicio de programación."""
    global _RUNNING
    with _LOCK:
        _RUNNING = False
    print("[SCHEDULER] Servicio de programación detenido.")


def is_scheduler_running() -> bool:
    with _LOCK:
        return _RUNNING
