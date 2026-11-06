"""
test_email_attachments.py — Pruebas de contrato para el arreglo de adjuntos.

Son la especificacion del arreglo: HOY deben FALLAR en los casos que el codigo
actual no cumple. No se modifica ningun archivo de produccion.

  A. invoice_manager.extract_attachments    -> que adjuntos se guardan
  B. invoice_manager.process_invoice        -> resultado por correo y reintentos
  C. invoice_manager.log / LOG_CALLBACK     -> reenvio de mensajes a la interfaz
  D. pipeline_orchestrator (ciclo completo) -> resumen "Fin de ciclo" y logs por correo

Aislamiento: sin red (Gmail, Gemini, Drive y portal son mocks), datos del usuario
redirigidos a una carpeta temporal y sin esperas reales (time.sleep simulado).

Ejecutar desde la carpeta del proyecto:
    python -m unittest test_email_attachments -v
"""

import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import data_manager
import invoice_manager
import pipeline_orchestrator


# ── Datos de prueba (falsos) ─────────────────────────────────────────────────

KB = 1024
PDF_BYTES = b"%PDF-1.4\n% factura de prueba\n%%EOF\n"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
ZIP_BYTES = b"PK\x03\x04" + b"\x00" * 64
SENDER = "Carrefour <facturas@example.com>"
SUBJECT = "Su factura de Carrefour"


def png_of_size(size):
    """PNG falso de tamaño exacto: cabecera PNG más relleno."""
    return PNG_SIGNATURE + b"\x00" * (size - len(PNG_SIGNATURE))


def make_part(payload, maintype, subtype, **kwargs):
    """Parte MIME adjunta. kwargs admitidos: disposition, filename, cid, params."""
    part = EmailMessage()
    part.set_content(payload, maintype=maintype, subtype=subtype, **kwargs)
    return part


def pdf_part(**kwargs):
    return make_part(PDF_BYTES, "application", "pdf", **kwargs)


def inline_pdf():
    """PDF 'inline' como en el correo real de Carrefour (el caso del bug)."""
    return pdf_part(disposition="inline", filename="13037232.pdf")


def inline_logo(size):
    """Imagen 'inline' con Content-ID, como un logo de cabecera de boletín."""
    return make_part(png_of_size(size), "image", "png",
                     disposition="inline", filename="logo.png", cid="<logo@example.com>")


def make_invoice_email(*parts, sender=SENDER, subject=SUBJECT):
    """Correo con estructura real: multipart/mixed (texto plano + HTML) más los adjuntos."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "usuario@example.com"
    msg["Subject"] = subject
    msg.set_content("Adjuntamos su factura.")
    msg.add_alternative("<html><body><p>Adjuntamos su factura.</p></body></html>", subtype="html")
    msg.make_mixed()
    for part in parts:
        msg.attach(part)
    return msg


def carrefour_info():
    """Lo que devuelve validate_is_invoice para una factura válida."""
    return {"supplier": "CARREFOUR", "date": "11-26", "amount": 216.32,
            "is_ambiguous": False, "ambiguous_options": []}


class IsolatedDataTestCase(unittest.TestCase):
    """Base común: datos en carpeta temporal, sin consola ni esperas, callback restaurado."""

    def setUp(self):
        # 1. Carpeta temporal para historial, cuarentena y reglas (nunca la carpeta data/ real)
        self.test_data_dir = tempfile.mkdtemp(prefix="test_email_data_")
        self.orig_data_dir = data_manager.DATA_DIR
        self.orig_history = data_manager.HISTORY_FILE
        self.orig_rules = data_manager.RULES_FILE
        self.orig_schedule = data_manager.SCHEDULE_FILE
        self.orig_quarantine = data_manager.QUARANTINE_DIR
        self.orig_log_callback = invoice_manager.LOG_CALLBACK

        data_manager.DATA_DIR = self.test_data_dir
        data_manager.QUARANTINE_DIR = os.path.join(self.test_data_dir, "quarantine")
        data_manager.HISTORY_FILE = os.path.join(self.test_data_dir, "invoices_history.json")
        data_manager.RULES_FILE = os.path.join(self.test_data_dir, "vendor_rules.json")
        data_manager.SCHEDULE_FILE = os.path.join(self.test_data_dir, "schedule_config.json")
        data_manager._ensure_dirs()

        # 2. Sin salida por consola (log() imprime) y sin esperas reales
        self.patch_attr(sys, "stdout", new=io.StringIO())
        self.patch_attr(time, "sleep")

    def tearDown(self):
        # Restaurar rutas originales y el callback de log; borrar la carpeta temporal
        data_manager.DATA_DIR = self.orig_data_dir
        data_manager.QUARANTINE_DIR = self.orig_quarantine
        data_manager.HISTORY_FILE = self.orig_history
        data_manager.RULES_FILE = self.orig_rules
        data_manager.SCHEDULE_FILE = self.orig_schedule
        invoice_manager.LOG_CALLBACK = self.orig_log_callback
        shutil.rmtree(self.test_data_dir, ignore_errors=True)

    def patch_attr(self, target, name, **kwargs):
        """Sustituye target.name solo durante este test."""
        patcher = patch.object(target, name, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked


# ── A. extract_attachments ───────────────────────────────────────────────────

class TestA_ExtractAttachments(IsolatedDataTestCase):
    """A. extract_attachments(msg, tmp_dir) devuelve la lista de rutas guardadas."""

    def setUp(self):
        super().setUp()
        # Carpeta de adjuntos a dos niveles de profundidad: un '../../' en el nombre
        # solo puede escribir dentro de base_dir, que se borra al terminar.
        self.base_dir = tempfile.mkdtemp(prefix="test_adjuntos_")
        self.addCleanup(shutil.rmtree, self.base_dir, ignore_errors=True)
        self.tmp_dir = os.path.join(self.base_dir, "nivel1", "nivel2")
        os.makedirs(self.tmp_dir)

    def extract(self, msg):
        return invoice_manager.extract_attachments(msg, self.tmp_dir)

    def assertSavedOne(self, saved):
        self.assertEqual(len(saved), 1, f"Se esperaba 1 adjunto guardado y hubo {len(saved)}: {saved}")
        self.assertTrue(os.path.isfile(saved[0]), f"La ruta devuelta no existe: {saved[0]}")

    def assertNothingSaved(self, saved):
        self.assertEqual(saved, [], f"No se esperaba ningún adjunto y se guardó: {saved}")

    def test_A01_pdf_inline_se_guarda(self):
        """PDF inline (caso Carrefour) se guarda."""
        self.assertSavedOne(self.extract(make_invoice_email(inline_pdf())))

    def test_A02_pdf_attachment_se_guarda(self):
        """PDF con disposición attachment se guarda."""
        part = pdf_part(disposition="attachment", filename="factura.pdf")
        self.assertSavedOne(self.extract(make_invoice_email(part)))

    def test_A03_pdf_con_nombre_sin_content_disposition_se_guarda(self):
        """PDF sin Content-Disposition pero con nombre en Content-Type se guarda."""
        part = pdf_part(params={"name": "factura_sin_disposicion.pdf"})
        self.assertSavedOne(self.extract(make_invoice_email(part)))

    def test_A04_pdf_sin_nombre_se_guarda_con_extension_pdf(self):
        """PDF sin ningún nombre se guarda con un nombre que acaba en .pdf."""
        saved = self.extract(make_invoice_email(pdf_part()))
        self.assertSavedOne(saved)
        self.assertTrue(saved[0].lower().endswith(".pdf"), f"El nombre no acaba en .pdf: {saved[0]}")

    def test_A05_logo_inline_menor_de_50kb_no_se_guarda(self):
        """Logo inline con Content-ID y menos de 50 KB no se guarda."""
        self.assertNothingSaved(self.extract(make_invoice_email(inline_logo(2 * KB))))

    def test_A06_imagen_inline_de_50kb_o_mas_se_guarda(self):
        """Mismo logo inline pero de 50 KB exactos: sí se guarda."""
        # 50 KB = 51 200 bytes: está en el límite con cualquier definición de KB
        self.assertSavedOne(self.extract(make_invoice_email(inline_logo(50 * KB))))

    def test_A07_png_pequeno_como_attachment_se_guarda(self):
        """PNG pequeño con disposición attachment se guarda."""
        part = make_part(png_of_size(2 * KB), "image", "png",
                         disposition="attachment", filename="captura.png")
        self.assertSavedOne(self.extract(make_invoice_email(part)))

    def test_A08_zip_como_attachment_no_se_guarda(self):
        """Adjunto .zip no se guarda."""
        part = make_part(ZIP_BYTES, "application", "zip",
                         disposition="attachment", filename="facturas.zip")
        self.assertNothingSaved(self.extract(make_invoice_email(part)))

    def test_A09_nombre_con_ruta_se_guarda_dentro_de_tmp_dir(self):
        """Nombre '../../evil.pdf' se guarda directamente dentro de tmp_dir."""
        part = pdf_part(disposition="attachment", filename="../../evil.pdf")
        saved = self.extract(make_invoice_email(part))
        self.assertSavedOne(saved)
        folder = os.path.normcase(os.path.dirname(os.path.abspath(saved[0])))
        self.assertEqual(folder, os.path.normcase(os.path.abspath(self.tmp_dir)),
                         f"El archivo se guardó fuera de la carpeta de adjuntos: {saved[0]}")

    def test_A10_disposicion_en_mayusculas_se_guarda(self):
        """Cabecera 'ATTACHMENT; filename=...' en mayúsculas se guarda."""
        part = pdf_part()
        part["Content-Disposition"] = 'ATTACHMENT; filename="factura_mayus.pdf"'
        self.assertSavedOne(self.extract(make_invoice_email(part)))


# ── B. process_invoice ───────────────────────────────────────────────────────

class TestB_ProcessInvoice(IsolatedDataTestCase):
    """B. process_invoice(...) devuelve el número de documentos a reintentar (int)."""

    def setUp(self):
        super().setUp()
        self.tmp_dir = tempfile.mkdtemp(prefix="test_email_tmp_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.mail = MagicMock(name="imap")
        self.gemini = MagicMock(name="gemini")
        self.drive = MagicMock(name="drive")
        # Lo que toca red o Drive se sustituye; la extracción y el historial son reales
        self.fetch = self.patch_attr(invoice_manager, "fetch_email")
        self.validate = self.patch_attr(invoice_manager, "validate_is_invoice")
        self.mark_read = self.patch_attr(invoice_manager, "mark_as_read")
        self.upload = self.patch_attr(invoice_manager, "upload_to_drive", return_value="id1")
        self.patch_attr(invoice_manager, "get_or_create_folder", return_value="carpeta-id")
        self.patch_attr(invoice_manager, "get_or_create_supplier_folder",
                        return_value="carpeta-proveedor-id")

    def run_process_invoice(self, msg):
        """Procesa el correo (UID '1') y devuelve lo que devuelve process_invoice."""
        self.fetch.return_value = msg
        return invoice_manager.process_invoice(
            self.mail, "1", self.gemini, self.drive, self.tmp_dir, reference_fps=None)

    def history(self):
        return data_manager.get_history(limit=500)

    def test_B1_correo_solo_con_logo_queda_como_NO_DOCUMENT(self):
        """Correo solo con logo: una entrada NO_DOCUMENT con asunto, remitente y motivo."""
        msg = make_invoice_email(inline_logo(2 * KB), subject="Novedades de Carrefour")
        result = self.run_process_invoice(msg)

        history = self.history()
        self.assertEqual(len(history), 1, f"Se esperaba 1 entrada en el historial y hay {len(history)}")
        entry = history[0]
        self.assertEqual(entry["status"], "NO_DOCUMENT")
        self.assertIn("Novedades de Carrefour", entry.get("email_subject") or "")
        self.assertIn("example.com", entry.get("sender") or "")
        self.assertTrue((entry.get("reason") or "").strip(), "El motivo del registro está vacío")
        self.mark_read.assert_called_once()
        self.assertEqual(result, 0)

    def test_B2_pdf_inline_no_factura_queda_DISCARDED_con_cuarentena(self):
        """PDF inline que Gemini descarta: una entrada DISCARDED con archivo de cuarentena."""
        self.validate.return_value = None
        result = self.run_process_invoice(make_invoice_email(inline_pdf()))

        history = self.history()
        self.assertEqual(len(history), 1, f"Se esperaba 1 entrada en el historial y hay {len(history)}")
        self.assertEqual(history[0]["status"], "DISCARDED")
        self.assertIsNotNone(history[0].get("quarantine_file"), "Falta el archivo de cuarentena")
        self.mark_read.assert_called_once()
        self.assertEqual(result, 0)

    def test_B3_pdf_inline_factura_valida_queda_SUCCESS(self):
        """PDF inline válido y subido a Drive: una entrada SUCCESS."""
        self.validate.return_value = carrefour_info()
        result = self.run_process_invoice(make_invoice_email(inline_pdf()))

        history = self.history()
        self.assertEqual(len(history), 1, f"Se esperaba 1 entrada en el historial y hay {len(history)}")
        self.assertEqual(history[0]["status"], "SUCCESS")
        self.mark_read.assert_called_once()
        self.assertEqual(result, 0)

    def test_B4_error_de_gemini_deja_el_correo_sin_marcar(self):
        """Error de API Gemini: el correo NO se marca como leído y devuelve 1."""
        self.validate.side_effect = invoice_manager.GeminiAPIError("Límite de peticiones")
        result = self.run_process_invoice(make_invoice_email(inline_pdf()))
        self.mark_read.assert_not_called()
        self.assertEqual(result, 1)

    def test_B5_error_al_subir_a_drive_deja_el_correo_sin_marcar(self):
        """Error genérico al subir a Drive: el correo NO se marca como leído y devuelve 1."""
        self.validate.return_value = carrefour_info()
        self.upload.side_effect = RuntimeError("Fallo de red al subir a Drive")
        result = self.run_process_invoice(make_invoice_email(inline_pdf()))
        self.mark_read.assert_not_called()
        self.assertEqual(result, 1)

    def test_B6_remitente_ignorado_se_marca_leido_sin_historial(self):
        """Dominio ignorado (enviomedical.com): se marca como leído, sin historial, devuelve 0."""
        self.patch_attr(invoice_manager, "IGNORED_SENDER_DOMAINS", new={"enviomedical.com"})
        msg = make_invoice_email(inline_pdf(), sender="EnvioMedical <pedidos@enviomedical.com>")
        result = self.run_process_invoice(msg)
        self.mark_read.assert_called_once()
        self.assertEqual(self.history(), [])
        self.assertEqual(result, 0)


# ── C. log y LOG_CALLBACK ────────────────────────────────────────────────────

class TestC_LogCallback(IsolatedDataTestCase):
    """C. log() reenvía cada mensaje a LOG_CALLBACK, salvo los separadores."""

    def test_C1_callback_recibe_mensaje_y_nivel(self):
        """Tras set_log_callback, log('hola', 'WARN') llama a cb('hola', 'WARN')."""
        cb = MagicMock(name="callback")
        invoice_manager.set_log_callback(cb)
        invoice_manager.log("hola", "WARN")
        cb.assert_called_once_with("hola", "WARN")

    def test_C2_separador_no_se_reenvia(self):
        """Una línea separadora no llama al callback."""
        cb = MagicMock(name="callback")
        invoice_manager.set_log_callback(cb)
        invoice_manager.log("\n" + "=" * 60)
        cb.assert_not_called()

    def test_C3_callback_que_falla_no_hace_fallar_log(self):
        """Si el callback lanza una excepción, log() no la propaga."""
        def callback_roto(msg, level="INFO"):
            raise RuntimeError("fallo en la interfaz")

        cb = MagicMock(name="callback", side_effect=callback_roto)
        invoice_manager.set_log_callback(cb)
        try:
            invoice_manager.log("hola")
        except Exception as exc:
            self.fail(f"log() no debe lanzar si el callback falla: {exc!r}")
        cb.assert_called_once()


# ── E. Hallazgos de la revisión independiente ────────────────────────────────

class TestE_RevisionIndependiente(TestB_ProcessInvoice):
    """E. Fallos permanentes y nombres raros no deben bloquear un correo para siempre."""

    def _validate_real(self, response, tax_id_map=None):
        """Ejecuta el validate_is_invoice real con un modelo de IA simulado."""
        # Sin depender del supplier_aliases.json local de cada instalación
        self.patch_attr(invoice_manager, "TAX_ID_SUPPLIER_MAP", new=tax_id_map or {})
        self.patch_attr(invoice_manager, "SUPPLIER_ALIASES", new={})
        pdf = os.path.join(self.tmp_dir, "doc.pdf")
        with open(pdf, "wb") as f:
            f.write(PDF_BYTES)
        model = MagicMock(name="modelo")
        model.generate_content.return_value = response
        self.patch_attr(invoice_manager, "pdfplumber", new=MagicMock())
        return self.validate_original(model, pdf, sender_email=SENDER, reference_fps=[])

    def setUp(self):
        self.validate_original = invoice_manager.validate_is_invoice
        super().setUp()

    def test_E1_ia_con_proveedor_y_fecha_nulos_no_lanza(self):
        """Respuesta de la IA con campos null: se procesa sin excepción."""
        response = MagicMock(text='{"is_invoice":"SI","supplier":null,"tax_id":"A28425270","date":null}')
        info = self._validate_real(response)
        self.assertEqual(info["supplier"], "DESCONOCIDO")

    def test_E1b_cif_asociado_unifica_el_nombre_del_proveedor(self):
        """El CIF A28425270 asociado a CARREFOUR sustituye el nombre largo que da la IA."""
        response = MagicMock(text='{"is_invoice":"SI","supplier":"C.C. CARREFOUR, S.A.","tax_id":"A28425270","date":"2026-10-30"}')
        info = self._validate_real(response, tax_id_map={"A28425270": "CARREFOUR"})
        self.assertEqual(info["supplier"], "CARREFOUR")

    def test_E2_respuesta_bloqueada_de_la_ia_se_descarta_sin_lanzar(self):
        """Si leer response.text falla (respuesta bloqueada), devuelve None para ir a Descartadas."""
        response = MagicMock()
        type(response).text = property(lambda self: (_ for _ in ()).throw(ValueError("bloqueada")))
        self.assertIsNone(self._validate_real(response))

    def test_E3_nombre_larguisimo_se_guarda(self):
        """Un nombre de 300 caracteres no impide guardar el PDF."""
        part = pdf_part(disposition="attachment", filename="a" * 300 + ".pdf")
        saved = invoice_manager.extract_attachments(make_invoice_email(part), self.tmp_dir)
        self.assertEqual(len(saved), 1)
        self.assertTrue(os.path.isfile(saved[0]))
        self.assertTrue(saved[0].endswith(".pdf"))

    def test_E4_nombre_reservado_de_windows_se_guarda(self):
        """Un adjunto llamado CON.pdf se guarda con un nombre seguro y legible."""
        part = pdf_part(disposition="attachment", filename="CON.pdf")
        saved = invoice_manager.extract_attachments(make_invoice_email(part), self.tmp_dir)
        self.assertEqual(len(saved), 1)
        self.assertNotEqual(os.path.basename(saved[0]).upper(), "CON.PDF")
        with open(saved[0], "rb") as f:
            self.assertEqual(f.read(), PDF_BYTES)

    def test_E5_fallo_de_historial_tras_subir_deja_el_correo_sin_leer(self):
        """Si no se puede anotar en el historial tras subir, el correo se reintenta."""
        self.validate.return_value = carrefour_info()
        self.patch_attr(data_manager, "add_history_entry", side_effect=OSError("disco lleno"))
        result = self.run_process_invoice(make_invoice_email(inline_pdf()))
        self.mark_read.assert_not_called()
        self.assertEqual(result, 1)


# ── D. Ciclo completo del pipeline ───────────────────────────────────────────

class TestD_PipelineSummary(IsolatedDataTestCase):
    """D. _execute_pipeline_worker: resumen 'Fin de ciclo' y logs de cada correo."""

    def setUp(self):
        super().setUp()
        # Estado global del orquestador: se guarda aquí y se restaura en tearDown
        self.orig_state = dict(pipeline_orchestrator._STATE)
        pipeline_orchestrator._STATE["logs"] = []

        # Dos correos: UID 1 = solo logo; UID 2 = PDF inline (Gemini falla con error de API)
        correos = {
            "1": make_invoice_email(inline_logo(2 * KB), subject="Novedades de Carrefour"),
            "2": make_invoice_email(inline_pdf()),
        }

        # Configuración falsa (no se lee el .env real)
        self.patch_attr(invoice_manager, "EMAIL_ADDRESS", new="usuario@example.com")
        self.patch_attr(invoice_manager, "APP_PASSWORD", new="clave-falsa")
        self.patch_attr(invoice_manager, "GEMINI_API_KEY", new="clave-falsa")
        self.patch_attr(invoice_manager, "DRIVE_ROOT_ID", new="carpeta-raiz-falsa")

        # Servicios externos sustituidos: ningún acceso a red
        self.patch_attr(invoice_manager, "init_gemini")
        self.patch_attr(invoice_manager, "authenticate_drive")
        self.patch_attr(invoice_manager, "connect_gmail")
        self.patch_attr(invoice_manager, "_load_reference_fingerprints", return_value=[])
        self.patch_attr(invoice_manager, "search_invoice_emails", return_value=["1", "2"])
        self.patch_attr(invoice_manager, "fetch_email", side_effect=lambda mail, uid: correos[uid])
        self.patch_attr(invoice_manager, "validate_is_invoice",
                        side_effect=invoice_manager.GeminiAPIError("Límite de peticiones simulado"))
        self.patch_attr(invoice_manager, "process_enviomedical")
        self.patch_attr(invoice_manager, "mark_as_read")
        self.patch_attr(invoice_manager, "upload_to_drive")
        self.patch_attr(invoice_manager, "get_or_create_folder")
        self.patch_attr(invoice_manager, "get_or_create_supplier_folder")

    def tearDown(self):
        # Devolver el estado del orquestador tal como estaba antes del test
        pipeline_orchestrator._STATE.clear()
        pipeline_orchestrator._STATE.update(self.orig_state)
        super().tearDown()

    def run_worker(self):
        """Ejecuta un ciclo completo (en el mismo hilo) y devuelve sus líneas de log."""
        pipeline_orchestrator._execute_pipeline_worker()
        return list(pipeline_orchestrator._STATE["logs"])

    def test_D1_fin_de_ciclo_cuenta_sin_documento_y_errores(self):
        """El resumen final cuenta 1 correo sin documento y 1 adjunto con error."""
        fin = [line for line in self.run_worker() if "Fin de ciclo" in line]
        self.assertTrue(fin, "No hay ninguna línea 'Fin de ciclo' en los logs")
        resumen = fin[-1]
        self.assertIn("1 correo(s) sin documento", resumen)
        self.assertIn("1 adjunto(s) con error", resumen)

    def test_D2_logs_de_cada_correo_llegan_al_estado(self):
        """Los mensajes de invoice_manager (p. ej. 'Procesando:') llegan a _STATE['logs']."""
        logs = self.run_worker()
        self.assertTrue(any("Procesando:" in line for line in logs),
                        "El texto 'Procesando:' de invoice_manager no llegó a _STATE['logs']")


if __name__ == "__main__":
    unittest.main()
