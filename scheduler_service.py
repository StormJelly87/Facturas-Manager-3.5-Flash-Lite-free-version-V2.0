"""
scheduler_service.py — Programador desatendido en segundo plano para el gestor de facturas.

Vigila la configuración en data/schedule_config.json y dispara ejecuciones
automáticas (semanales, mensuales o diarias) sin necesidad de configurar tareas
del sistema operativo.
"""

import time
import threading
from datetime import datetime

import data_manager
import pipeline_orchestrator

_SCHEDULER_THREAD = None
_RUNNING = False
_LOCK = threading.Lock()


def _scheduler_loop() -> None:
    """Bucle del programador que verifica cada 30 segundos si toca ejecutar."""
    global _RUNNING
    last_triggered_key = None

    while True:
        with _LOCK:
            if not _RUNNING:
                break

        try:
            config = data_manager.load_schedule_config()
            if config.get("enabled"):
                now = datetime.now()
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
                        target_dow = int(config.get("day_of_week", 0))  # 0 = Lunes
                        if now.weekday() == target_dow:
                            trigger_key = f"weekly_{now.strftime('%Y%W_%H%M')}"
                            should_run = True
                    elif freq == "monthly":
                        target_dom = int(config.get("day_of_month", 1))
                        if now.day == target_dom:
                            trigger_key = f"monthly_{now.strftime('%Y%m_%H%M')}"
                            should_run = True

                # Disparar solo una vez en ese minuto
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


def start_scheduler() -> None:
    """Inicia el servicio de programación en segundo plano."""
    global _SCHEDULER_THREAD, _RUNNING
    with _LOCK:
        if _RUNNING:
            return
        _RUNNING = True
        _SCHEDULER_THREAD = threading.Thread(target=_scheduler_loop, daemon=True)
        _SCHEDULER_THREAD.start()
        print("[SCHEDULER] Servicio de programación iniciado en segundo plano.")


def stop_scheduler() -> None:
    """Detiene el servicio de programación."""
    global _RUNNING
    with _LOCK:
        _RUNNING = False
    print("[SCHEDULER] Servicio de programación detenido.")


def is_scheduler_running() -> bool:
    with _LOCK:
        return _RUNNING
