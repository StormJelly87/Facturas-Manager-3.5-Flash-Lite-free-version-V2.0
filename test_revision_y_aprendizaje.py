"""
test_revision_y_aprendizaje.py — Pruebas de contrato (primero las pruebas, luego el código).

Describen la funcionalidad NUEVA de revisión y aprendizaje:
  - Documentos sin CIF o sin fecha de un proveedor ya conocido van a 'Dudosas'
    (REVIEW_MISSING_DATA) en vez de a 'Descartadas'.
  - Formatos de documento ya rechazados ('confirmacionDelPedido_<número>.pdf') se
    aprenden y se ignoran en correos futuros (IGNORED_KNOWN_FORMAT).
  - Memoria de correos ya procesados, para no volver a tratar correos abiertos.
  - Rescate manual sin crear reglas automáticas por proveedor.

HOY la mayoría de estas pruebas deben FALLAR, porque la función o el comportamiento
todavía no existen. Algunas pruebas "de guarda" ya pasan porque protegen un
comportamiento actual que la implementación no debe romper. No se modifica ningún
archivo de producción.

  F. data_manager          -> proveedor conocido, formatos rechazados, correos
                              procesados, cuarentena y estadísticas
  G. invoice_manager       -> lectura sin marcar como leído y motivo del rechazo de la IA
  H. process_invoice       -> qué hacer con cada motivo de rechazo y memoria de correos
  I. search_invoice_emails -> correos abiertos (línea base) y no leídos
  J. pipeline_orchestrator -> resumen "Fin de ciclo" y sesión vacía
  K. dashboard (FastAPI)   -> endpoints de revisión, formatos rechazados y rescate
  L. clave de correo y ventana de lectura

Aislamiento: sin red (Gmail, Gemini, Drive y portal son mocks), datos del usuario
redirigidos a una carpeta temporal y sin esperas reales (time.sleep simulado).

Ejecutar desde la carpeta del proyecto:
    python -W ignore -m unittest test_revision_y_aprendizaje -v
"""

import imaplib
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import data_manager
import invoice_manager
import pipeline_orchestrator
from dashboard.app import app
from test_email_attachments import (
    KB,
    PDF_BYTES,
    SENDER,
    SUBJECT,
    IsolatedDataTestCase,
    carrefour_info,
    inline_logo,
    inline_pdf,
    make_invoice_email,
    pdf_part,
)


# ── Datos de prueba (falsos) ─────────────────────────────────────────────────

REMITENTE_CARREFOUR_ENVIOS = "Carrefour <carrefour.es@enviosemail.carrefour.es>"
REMITENTE_NUEVO = "Proveedor Nuevo <pedidos@proveedor-nuevo.com>"
PEDIDO_CARREFOUR = "confirmacionDelPedido_13037232.pdf"
FECHA_ISO = "2026-10-30"

# validate_is_invoice real, guardado al cargar este módulo (antes de que ningún test lo sustituya)
VALIDATE_REAL = invoice_manager.validate_is_invoice


# ── Ayudas comunes ───────────────────────────────────────────────────────────

def _pdfplumber_falso(texto=None):
    """Sustituto de pdfplumber. Con 'texto', la única página devuelve ese texto; sin él, no hay páginas."""
    falso = MagicMock(name="pdfplumber")
    if texto is not None:
        pagina = MagicMock(name="pagina")
        pagina.extract_text.return_value = texto
        documento = MagicMock(name="documento")
        documento.pages = [pagina]
        falso.open.return_value.__enter__.return_value = documento
    return falso


def _respuesta_ia(texto):
    """Respuesta falsa de Gemini: solo expone 'text', igual que la API real."""
    return MagicMock(name="respuesta", text=texto)


def _uids(lista):
    """Línea de búsqueda de IMAP ('1 2 3') en bytes; vacía si no hay resultados."""
    return " ".join(str(u) for u in lista).encode()


def _texto(valor):
    return valor.decode() if isinstance(valor, bytes) else str(valor)


def _fetch_cabecera(uid, message_id):
    """Trozos de respuesta FETCH con la cabecera Message-ID, con el formato que devuelve imaplib."""
    cabecera = (f"Message-ID: {message_id}\r\n\r\n" if message_id else "\r\n").encode()
    meta = f"{uid} (UID {uid} BODY[HEADER.FIELDS (MESSAGE-ID)] {{{len(cabecera)}}}".encode()
    return [(meta, cabecera), b")"]


def _ids_de_respuesta(datos):
    """Acepta una lista de registros o un objeto que contiene una lista; devuelve el conjunto de ids."""
    if isinstance(datos, dict):
        datos = next((v for v in datos.values() if isinstance(v, list)), [])
    return {e.get("id") for e in datos if isinstance(e, dict)}


def _ia_de_dos_correos(*args, **kwargs):
    """IA falsa del ciclo completo: el pedido de Carrefour no es factura; el otro documento
    es una factura sin CIF de un proveedor ya conocido."""
    ruta = args[1] if len(args) > 1 else kwargs.get("filepath", "")
    rejection = kwargs.get("rejection")
    if isinstance(rejection, dict):
        if "confirmacion" in os.path.basename(ruta).lower():
            rejection["kind"] = "AI_NO"
        else:
            rejection.update(kind="NO_TAX_ID", supplier="C.C. CARREFOUR, S.A.", date=FECHA_ISO)
    return None


# ── F. data_manager ──────────────────────────────────────────────────────────

class TestF_DatosDeRevision(IsolatedDataTestCase):
    """F. data_manager: proveedor conocido, formatos rechazados, correos procesados y cuarentena."""

    def historial(self, status, sender, supplier, **extra):
        """Añade una entrada al historial (en la carpeta temporal) y devuelve su id."""
        return data_manager.add_history_entry(
            status=status, filename="factura.pdf", supplier=supplier,
            date_str="10-26", sender=sender, **extra)

    def ruta_datos(self, nombre):
        """Ruta de un archivo de datos en la carpeta temporal (se calcula en cada uso)."""
        return os.path.join(data_manager.DATA_DIR, nombre)

    # ── Proveedor conocido ──

    def test_F01_mismo_correo_del_remitente_es_proveedor_conocido(self):
        """Un remitente cuya dirección ya tiene un registro válido es conocido, sea cual sea el nombre."""
        self.historial("SUCCESS", "Carrefour <facturas@example.com>", "CARREFOUR")
        self.assertTrue(data_manager.is_known_supplier("Otro Nombre <FACTURAS@example.com>", None))

    def test_F02_remitente_nuevo_sin_proveedor_util_no_es_conocido(self):
        """Sin dirección conocida y sin nombre útil (vacío, nulo o DESCONOCIDO), no es conocido."""
        self.historial("SUCCESS", "Carrefour <facturas@example.com>", "CARREFOUR")
        for proveedor in (None, "", "DESCONOCIDO"):
            with self.subTest(proveedor=proveedor):
                self.assertFalse(data_manager.is_known_supplier("nuevo@otro.com", proveedor))

    def test_F03_nombre_del_historial_como_palabra_completa(self):
        """'CARREFOUR' del historial coincide con 'C.C. CARREFOUR, S.A.' aunque el correo sea nuevo."""
        self.historial("SUCCESS", "otro@proveedor.es", "CARREFOUR")
        self.assertTrue(data_manager.is_known_supplier("nuevo@otro.com", "C.C. CARREFOUR, S.A."))

    def test_F04_nombre_dentro_de_otra_palabra_no_coincide(self):
        """'CARREFOUR' no coincide con 'MEGACARREFOUREXPRESS': no es una palabra completa."""
        self.historial("SUCCESS", "otro@proveedor.es", "CARREFOUR")
        self.assertFalse(data_manager.is_known_supplier("nuevo@otro.com", "MEGACARREFOUREXPRESS SL"))

    def test_F05_nombre_de_menos_de_4_letras_nunca_coincide(self):
        """Un nombre de 3 letras en el historial nunca identifica a un proveedor."""
        self.historial("SUCCESS", "otro@proveedor.es", "ABC")
        self.assertFalse(data_manager.is_known_supplier("nuevo@otro.com", "ABC SERVICIOS SL"))

    def test_F06_descartados_y_sin_documento_no_cuentan(self):
        """Los registros DISCARDED y NO_DOCUMENT no hacen conocido a nadie."""
        self.historial("DISCARDED", "a@x.com", "CARREFOUR")
        self.historial("NO_DOCUMENT", "b@x.com", "LIDL SUPERMERCADOS")
        self.assertFalse(data_manager.is_known_supplier("a@x.com", None))
        self.assertFalse(data_manager.is_known_supplier("b@x.com", None))
        self.assertFalse(data_manager.is_known_supplier("nuevo@otro.com", "C.C. CARREFOUR, S.A."))
        self.assertFalse(data_manager.is_known_supplier("nuevo@otro.com", "LIDL SUPERMERCADOS SAU"))

    def test_F07_estados_validos_hacen_conocido_al_remitente(self):
        """SUCCESS, AMBIGUOUS_DATE y SKIPPED_DUPLICATE sí sirven para reconocer al remitente."""
        for estado in ("SUCCESS", "AMBIGUOUS_DATE", "SKIPPED_DUPLICATE"):
            with self.subTest(estado=estado):
                remitente = f"{estado.lower()}@proveedor.es"
                self.historial(estado, remitente, f"PROVEEDOR {estado}")
                self.assertTrue(data_manager.is_known_supplier(remitente, None))

    def test_F08_desconocido_del_historial_no_identifica_a_nadie(self):
        """Un registro con proveedor 'DESCONOCIDO' no hace conocido al siguiente correo sin nombre."""
        self.historial("SUCCESS", "otro@proveedor.es", "DESCONOCIDO")
        self.assertFalse(data_manager.is_known_supplier("nuevo@otro.com", "DESCONOCIDO"))

    def test_F09_regla_aprendida_tambien_identifica_al_proveedor(self):
        """Una regla guardada para un proveedor cuenta como proveedor conocido (palabra completa)."""
        data_manager.save_vendor_rule("Acme Supplies")
        self.assertTrue(data_manager.is_known_supplier("nuevo@otro.com", "ACME SUPPLIES, S.L."))
        self.assertFalse(data_manager.is_known_supplier("nuevo@otro.com", "OTRO PROVEEDOR SA"))

    # ── Formatos de documento rechazados ──

    def test_F10_clave_de_formato_sustituye_los_numeros(self):
        """La clave de formato cambia cada número por '#' y pasa el nombre a minúsculas."""
        self.assertEqual(data_manager.document_format_key("confirmacionDelPedido_13037232.pdf"),
                         "confirmaciondelpedido_#.pdf")
        self.assertEqual(data_manager.document_format_key("13037232.pdf"), "#.pdf")
        self.assertEqual(data_manager.document_format_key("Factura-2026-001.PDF"), "factura-#-#.pdf")
        self.assertEqual(data_manager.document_format_key("confirmacionDelPedido_99.pdf"),
                         data_manager.document_format_key(PEDIDO_CARREFOUR))

    def test_F11_aprender_un_formato_lo_guarda_en_disco(self):
        """Aprender un formato devuelve su 'id' y lo guarda en rejected_formats.json."""
        registro = data_manager.learn_rejected_format(REMITENTE_NUEVO, PEDIDO_CARREFOUR)
        self.assertIn("id", registro)
        self.assertTrue(os.path.exists(self.ruta_datos("rejected_formats.json")))
        self.assertEqual([r["id"] for r in data_manager.list_rejected_formats()], [registro["id"]])

    def test_F12_mismo_remitente_y_formato_no_se_duplica(self):
        """Aprender dos veces el mismo remitente y formato (con otro número) deja una sola entrada."""
        data_manager.learn_rejected_format(REMITENTE_NUEVO, "confirmacionDelPedido_1.pdf")
        data_manager.learn_rejected_format(REMITENTE_NUEVO, "confirmacionDelPedido_2.pdf")
        self.assertEqual(len(data_manager.list_rejected_formats()), 1)

    def test_F13_formato_aprendido_coincide_con_otro_numero_del_mismo_remitente(self):
        """Tras aprender un formato, otro número del mismo remitente también coincide (y mayúsculas)."""
        data_manager.learn_rejected_format(REMITENTE_NUEVO, PEDIDO_CARREFOUR)
        self.assertTrue(data_manager.is_rejected_format(REMITENTE_NUEVO, "confirmacionDelPedido_99999999.pdf"))
        self.assertTrue(data_manager.is_rejected_format(
            REMITENTE_NUEVO.replace("Proveedor Nuevo", "Otro"), PEDIDO_CARREFOUR))
        self.assertTrue(data_manager.is_rejected_format(REMITENTE_NUEVO, "CONFIRMACIONDELPEDIDO_5.PDF"))

    def test_F14_otro_remitente_u_otro_formato_no_coinciden(self):
        """El formato aprendido solo vale para ese remitente y ese formato."""
        data_manager.learn_rejected_format(REMITENTE_NUEVO, PEDIDO_CARREFOUR)
        self.assertFalse(data_manager.is_rejected_format("Otro <otro@tienda.es>", PEDIDO_CARREFOUR))
        self.assertFalse(data_manager.is_rejected_format(REMITENTE_NUEVO, "factura_13037232.pdf"))

    def test_F15_regla_integrada_de_carrefour_sin_ningun_archivo(self):
        """Carrefour (cualquiera de sus dominios) con 'confirmacionDelPedido_#.pdf' ya está rechazado,
        aunque no se haya aprendido nada."""
        self.assertFalse(os.path.exists(self.ruta_datos("rejected_formats.json")))
        self.assertTrue(data_manager.is_rejected_format(REMITENTE_CARREFOUR_ENVIOS, PEDIDO_CARREFOUR))
        self.assertTrue(data_manager.is_rejected_format(
            "Carrefour <clientes_carrefour.es@clientes.carrefour.com>", PEDIDO_CARREFOUR))
        self.assertFalse(data_manager.is_rejected_format("Proveedor <pedidos@proveedor.com>", PEDIDO_CARREFOUR))

    def test_F16_olvidar_un_formato_lo_quita_de_la_lista(self):
        """forget_rejected_format quita el formato aprendido; con un id ya olvidado devuelve False."""
        registro = data_manager.learn_rejected_format(REMITENTE_NUEVO, PEDIDO_CARREFOUR)
        self.assertTrue(data_manager.forget_rejected_format(registro["id"]))
        self.assertEqual(data_manager.list_rejected_formats(), [])
        self.assertFalse(data_manager.is_rejected_format(REMITENTE_NUEVO, PEDIDO_CARREFOUR))
        self.assertFalse(data_manager.forget_rejected_format(registro["id"]))

    # ── Correos ya procesados ──

    def test_F17_lista_de_correos_procesados_es_nula_sin_archivo(self):
        """Sin archivo de correos procesados, la lista devuelve None; guardada, se lee igual."""
        self.assertIsNone(data_manager.load_processed_email_ids())
        data_manager.save_processed_email_ids({"a@b.com", "c@d.com"})
        self.assertEqual(data_manager.load_processed_email_ids(), {"a@b.com", "c@d.com"})

    def test_F18_recordar_un_correo_lo_anade_si_el_archivo_existe(self):
        """remember_processed_email añade la clave y la guarda cuando el archivo ya existe."""
        data_manager.save_processed_email_ids({"a@b.com"})
        data_manager.remember_processed_email("c@d.com")
        self.assertEqual(data_manager.load_processed_email_ids(), {"a@b.com", "c@d.com"})

    def test_F19_recordar_un_correo_sin_archivo_no_lo_crea(self):
        """Sin archivo previo, remember_processed_email no hace nada: el archivo no aparece."""
        data_manager.remember_processed_email("a@b.com")
        self.assertFalse(os.path.exists(self.ruta_datos("processed_emails.json")))
        self.assertIsNone(data_manager.load_processed_email_ids())

    # ── Cuarentena y estadísticas ──

    def test_F20_revision_de_datos_guarda_copia_en_cuarentena(self):
        """Un registro REVIEW_MISSING_DATA guarda copia del documento en cuarentena, como DISCARDED."""
        origen = os.path.join(self.test_data_dir, "origen.pdf")
        with open(origen, "wb") as f:
            f.write(PDF_BYTES)
        entry_id = data_manager.add_history_entry(
            status="REVIEW_MISSING_DATA", filename=PEDIDO_CARREFOUR, supplier="CARREFOUR",
            date_str="10-26", original_filepath=origen, sender=REMITENTE_NUEVO)
        ruta = data_manager.get_quarantine_path(entry_id)
        self.assertIsNotNone(ruta, "REVIEW_MISSING_DATA debe guardar copia en cuarentena")
        self.assertTrue(os.path.exists(ruta))

    def test_F21_estadisticas_la_revision_cuenta_como_dudosa(self):
        """REVIEW_MISSING_DATA suma en dudosas y en 'requiere revisión'; IGNORED_KNOWN_FORMAT no suma."""
        self.historial("SUCCESS", "a@x.com", "CARREFOUR")
        self.historial("REVIEW_MISSING_DATA", "b@x.com", "C.C. CARREFOUR")
        self.historial("DISCARDED", "c@x.com", "OTRO PROVEEDOR")
        self.historial("IGNORED_KNOWN_FORMAT", "d@x.com", "CARREFOUR")
        self.historial("IGNORED_KNOWN_FORMAT", "d@x.com", "CARREFOUR")
        stats = data_manager.get_stats()
        self.assertEqual(stats["total_processed"], 3)
        self.assertEqual(stats["success_count"], 1)
        self.assertEqual(stats["discarded_count"], 1)
        self.assertEqual(stats["ambiguous_count"], 1)
        self.assertEqual(stats["requires_review"], 2)

    def test_F22_los_ignorados_no_cuentan_en_ningun_total(self):
        """Solo entradas IGNORED_KNOWN_FORMAT: todos los totales de revisión son cero."""
        self.historial("IGNORED_KNOWN_FORMAT", "d@x.com", "CARREFOUR")
        stats = data_manager.get_stats()
        self.assertEqual(stats["total_processed"], 0)
        self.assertEqual(stats["discarded_count"], 0)
        self.assertEqual(stats["ambiguous_count"], 0)
        self.assertEqual(stats["requires_review"], 0)


# ── G. invoice_manager: lectura y motivo del rechazo ─────────────────────────

class TestG_LecturaYMotivoDeRechazo(IsolatedDataTestCase):
    """G. invoice_manager: lectura de correos sin marcarlos y motivo del rechazo de la IA."""

    def setUp(self):
        super().setUp()
        self.tmp_dir = tempfile.mkdtemp(prefix="test_revision_g_")
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        # Sin depender de un supplier_aliases.json local de cada instalación
        self.patch_attr(invoice_manager, "TAX_ID_SUPPLIER_MAP", new={})
        self.patch_attr(invoice_manager, "SUPPLIER_ALIASES", new={})

    def validar(self, respuesta_ia, texto_pdf=None):
        """Ejecuta validate_is_invoice real con una IA simulada. Devuelve (resultado, rejection)."""
        pdf = os.path.join(self.tmp_dir, "documento.pdf")
        with open(pdf, "wb") as f:
            f.write(PDF_BYTES)
        modelo = MagicMock(name="modelo")
        modelo.generate_content.return_value = _respuesta_ia(respuesta_ia)
        self.patch_attr(invoice_manager, "pdfplumber", new=_pdfplumber_falso(texto_pdf))
        rejection = {}
        resultado = VALIDATE_REAL(modelo, pdf, sender_email=SENDER, reference_fps=[], rejection=rejection)
        return resultado, rejection

    def test_G01_descargar_un_correo_no_lo_marca_como_leido(self):
        """fetch_email pide BODY.PEEK[] y no RFC822: un correo que falla no queda marcado como leído."""
        raw = make_invoice_email(inline_pdf()).as_bytes()
        buzon = MagicMock(name="imap")
        buzon.uid.return_value = ("OK", [(b"1 (BODY[] {%d}" % len(raw), raw), b")"])
        msg = invoice_manager.fetch_email(buzon, "1")
        argumentos = buzon.uid.call_args[0]
        self.assertIn("(BODY.PEEK[])", argumentos)
        self.assertFalse(any("RFC822" in _texto(a).upper() for a in argumentos),
                         f"Se pidió RFC822 (marca el correo como leído): {argumentos}")
        self.assertEqual(msg["Subject"], SUBJECT)

    def test_G02_la_ia_dice_no_motivo_AI_NO(self):
        """La IA responde is_invoice NO: el motivo es AI_NO."""
        resultado, rejection = self.validar(
            '{"is_invoice":"NO","supplier":null,"tax_id":"DESCONOCIDO","date":null}',
            texto_pdf="Confirmacion de pedido n. 13037232")
        self.assertIsNone(resultado)
        self.assertEqual(rejection.get("kind"), "AI_NO")

    def test_G03_sin_cif_motivo_NO_TAX_ID_con_proveedor_y_fecha(self):
        """Sin CIF válido: motivo NO_TAX_ID, guardando el proveedor y la fecha que dio la IA."""
        resultado, rejection = self.validar(
            '{"is_invoice":"SI","supplier":"C.C. CARREFOUR, S.A.","tax_id":"DESCONOCIDO","date":"2026-10-30"}')
        self.assertIsNone(resultado)
        self.assertEqual(rejection.get("kind"), "NO_TAX_ID")
        self.assertEqual(rejection.get("supplier"), "C.C. CARREFOUR, S.A.")
        self.assertEqual(rejection.get("date"), FECHA_ISO)

    def test_G04_sin_fecha_motivo_NO_DATE(self):
        """CIF válido, pero el texto del PDF no tiene ninguna fecha: motivo NO_DATE."""
        resultado, rejection = self.validar(
            '{"is_invoice":"SI","supplier":"CARREFOUR","tax_id":"B12345678","date":"2026-10-30"}',
            texto_pdf="Documento de compra total a pagar euros")
        self.assertIsNone(resultado)
        self.assertEqual(rejection.get("kind"), "NO_DATE")
        self.assertEqual(rejection.get("supplier"), "CARREFOUR")
        self.assertEqual(rejection.get("date"), FECHA_ISO)

    def test_G05_respuesta_que_no_es_json_motivo_AI_ERROR(self):
        """Si la IA no responde JSON válido, el motivo es AI_ERROR (no es un 'no' de la IA)."""
        resultado, rejection = self.validar("Lo siento, no puedo leer este documento.")
        self.assertIsNone(resultado)
        self.assertEqual(rejection.get("kind"), "AI_ERROR")

    def test_G06_factura_valida_se_devuelve_sin_motivo_de_rechazo(self):
        """Una factura válida devuelve sus datos y no lleva ningún motivo de rechazo."""
        resultado, rejection = self.validar(
            '{"is_invoice":"SI","supplier":"CARREFOUR","tax_id":"A28425270","date":"2026-10-30"}',
            texto_pdf="Factura CARREFOUR 30/10/2026 CIF A28425270 total 10,00 euros")
        self.assertIsInstance(resultado, dict)
        self.assertEqual(resultado["supplier"], "CARREFOUR")
        self.assertNotIn("kind", rejection)


# ── H. process_invoice: motivos de rechazo y memoria de correos ──────────────

class TestH_ProcessInvoiceRevision(IsolatedDataTestCase):
    """H. process_invoice: qué hacer con cada motivo de rechazo y qué correos se recuerdan."""

    def setUp(self):
        super().setUp()
        self.tmp_dir = tempfile.mkdtemp(prefix="test_revision_h_")
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

    # ── Ayudas ──

    def correo(self, *partes, remitente=SENDER, asunto=SUBJECT, message_id=None):
        """Correo de prueba con sus adjuntos y, opcionalmente, una cabecera Message-ID."""
        msg = make_invoice_email(*partes, sender=remitente, subject=asunto)
        if message_id is not None:
            msg["Message-ID"] = message_id
        return msg

    def procesar(self, msg, uid="1"):
        """Procesa el correo con process_invoice real y devuelve su resultado (número de fallos)."""
        self.fetch.return_value = msg
        return invoice_manager.process_invoice(
            self.mail, uid, self.gemini, self.drive, self.tmp_dir, reference_fps=None)

    def ia_descarta(self, kind, supplier=None, date=None):
        """La IA devuelve None (descarta el documento) y deja el motivo en el diccionario 'rejection'."""
        def _descarte(*args, **kwargs):
            rejection = kwargs.get("rejection")
            if isinstance(rejection, dict):
                rejection["kind"] = kind
                if supplier is not None:
                    rejection["supplier"] = supplier
                if date is not None:
                    rejection["date"] = date
            return None
        self.validate.side_effect = _descarte

    def sembrar_proveedor_conocido(self):
        """Carrefour ya tiene un documento archivado: su remitente y su nombre son conocidos."""
        data_manager.add_history_entry(status="SUCCESS", filename="previa.pdf", supplier="CARREFOUR",
                                       date_str="10-26", sender=SENDER)

    def entradas(self, status):
        return data_manager.get_history(status=status, limit=500)

    def ruta_datos(self, nombre):
        return os.path.join(data_manager.DATA_DIR, nombre)

    def correos_recordados(self):
        return data_manager.load_processed_email_ids()

    def comprobar_revision(self, kind):
        """Proveedor conocido + motivo de datos incompletos: una entrada REVIEW_MISSING_DATA con copia."""
        self.sembrar_proveedor_conocido()
        self.ia_descarta(kind, supplier="C.C. CARREFOUR, S.A.", date=FECHA_ISO)
        resultado = self.procesar(self.correo(inline_pdf()))

        revisiones = self.entradas("REVIEW_MISSING_DATA")
        self.assertEqual(len(revisiones), 1, f"Se esperaba 1 REVIEW_MISSING_DATA y hay {len(revisiones)}")
        entrada = revisiones[0]
        self.assertEqual(entrada["supplier"], "C.C. CARREFOUR, S.A.")
        self.assertEqual(entrada["detected_dates"], [{"iso": FECHA_ISO}])
        self.assertIsNotNone(entrada.get("quarantine_file"), "Falta el archivo de cuarentena")
        self.assertTrue(os.path.exists(os.path.join(data_manager.QUARANTINE_DIR, entrada["quarantine_file"])))
        self.assertEqual(self.entradas("DISCARDED"), [])
        self.upload.assert_not_called()
        self.mark_read.assert_called_once()
        self.assertEqual(resultado, 0)

    # ── Motivos de rechazo ──

    def test_H01_formato_rechazado_de_carrefour_se_ignora_sin_cuarentena(self):
        """Pedido de Carrefour con formato ya rechazado: IGNORED_KNOWN_FORMAT, sin copia ni descarte."""
        self.ia_descarta("AI_NO")
        msg = self.correo(pdf_part(disposition="inline", filename=PEDIDO_CARREFOUR),
                          remitente=REMITENTE_CARREFOUR_ENVIOS, asunto="Confirmacion de pedido")
        resultado = self.procesar(msg)

        historial = data_manager.get_history(limit=500)
        self.assertEqual(len(historial), 1, f"Se esperaba 1 entrada y hay {len(historial)}")
        self.assertEqual(historial[0]["status"], "IGNORED_KNOWN_FORMAT")
        self.assertFalse(historial[0].get("quarantine_file"), "Un documento ignorado no debe copiarse a cuarentena")
        self.assertEqual(os.listdir(data_manager.QUARANTINE_DIR), [])
        self.assertEqual(self.entradas("DISCARDED"), [])
        self.upload.assert_not_called()
        self.mark_read.assert_called_once()
        self.assertEqual(resultado, 0)

    def test_H02_error_de_la_ia_no_se_ignora_aunque_el_formato_este_rechazado(self):
        """Si la IA falla (AI_ERROR), el documento va a Descartadas aunque su formato esté rechazado."""
        self.ia_descarta("AI_ERROR")
        msg = self.correo(pdf_part(disposition="inline", filename=PEDIDO_CARREFOUR),
                          remitente=REMITENTE_CARREFOUR_ENVIOS)
        resultado = self.procesar(msg)

        self.assertEqual([e["status"] for e in data_manager.get_history(limit=500)], ["DISCARDED"])
        self.assertIsNotNone(self.entradas("DISCARDED")[0].get("quarantine_file"))
        self.assertEqual(resultado, 0)

    def test_H03_formato_aprendido_de_otro_remitente_se_ignora(self):
        """Un formato aprendido de un remitente se ignora en sus correos siguientes (con otro número)."""
        data_manager.learn_rejected_format(REMITENTE_NUEVO, PEDIDO_CARREFOUR)
        self.ia_descarta("AI_NO")
        msg = self.correo(pdf_part(disposition="inline", filename="confirmacionDelPedido_55555555.pdf"),
                          remitente=REMITENTE_NUEVO)
        resultado = self.procesar(msg)

        self.assertEqual([e["status"] for e in data_manager.get_history(limit=500)], ["IGNORED_KNOWN_FORMAT"])
        self.assertEqual(self.entradas("DISCARDED"), [])
        self.mark_read.assert_called_once()
        self.assertEqual(resultado, 0)

    def test_H04_no_factura_de_proveedor_conocido_sigue_en_descartadas(self):
        """Un 'NO' de la IA se descarta como siempre, aunque el proveedor sea conocido."""
        self.sembrar_proveedor_conocido()
        self.ia_descarta("AI_NO")
        resultado = self.procesar(self.correo(inline_pdf()))

        self.assertEqual(len(self.entradas("DISCARDED")), 1)
        self.assertEqual(self.entradas("REVIEW_MISSING_DATA"), [])
        self.assertEqual(resultado, 0)

    def test_H05_sin_cif_de_proveedor_desconocido_se_descarta(self):
        """Sin CIF y con proveedor desconocido: se descarta, no hay nada que revisar."""
        self.ia_descarta("NO_TAX_ID", supplier="PROVEEDOR NUEVO SL", date=FECHA_ISO)
        resultado = self.procesar(self.correo(inline_pdf(), remitente=REMITENTE_NUEVO))

        self.assertEqual(len(self.entradas("DISCARDED")), 1)
        self.assertEqual(self.entradas("REVIEW_MISSING_DATA"), [])
        self.assertEqual(resultado, 0)

    def test_H06_sin_cif_de_proveedor_conocido_va_a_revision_de_datos(self):
        """Sin CIF pero con proveedor conocido: REVIEW_MISSING_DATA con copia, sin subir a Drive."""
        self.comprobar_revision("NO_TAX_ID")

    def test_H07_sin_fecha_de_proveedor_conocido_va_a_revision_de_datos(self):
        """Sin fecha pero con proveedor conocido: REVIEW_MISSING_DATA con copia, sin subir a Drive."""
        self.comprobar_revision("NO_DATE")

    def test_H08_error_de_la_ia_de_proveedor_conocido_se_descarta(self):
        """Un error de la IA nunca va a revisión de datos: se descarta."""
        self.sembrar_proveedor_conocido()
        self.ia_descarta("AI_ERROR")
        resultado = self.procesar(self.correo(inline_pdf()))

        self.assertEqual(len(self.entradas("DISCARDED")), 1)
        self.assertEqual(self.entradas("REVIEW_MISSING_DATA"), [])
        self.assertEqual(resultado, 0)

    def test_H09_factura_aceptada_no_se_ve_afectada_por_formatos_rechazados(self):
        """Si la IA acepta el documento, un formato rechazado para ese remitente no lo ignora."""
        data_manager.learn_rejected_format(SENDER, "13037232.pdf")
        self.validate.return_value = carrefour_info()
        resultado = self.procesar(self.correo(inline_pdf()))

        self.assertEqual([e["status"] for e in data_manager.get_history(limit=500)], ["SUCCESS"])
        self.upload.assert_called_once()
        self.mark_read.assert_called_once()
        self.assertEqual(resultado, 0)

    # ── Memoria de correos procesados ──

    def test_H10_correo_archivado_se_recuerda_por_su_message_id(self):
        """Un correo procesado sin fallos se recuerda con su Message-ID, sin corchetes."""
        data_manager.save_processed_email_ids(set())
        self.validate.return_value = carrefour_info()
        resultado = self.procesar(self.correo(inline_pdf(), message_id="<factura-1@proveedor.com>"))

        self.assertEqual(resultado, 0)
        self.assertIn("factura-1@proveedor.com", self.correos_recordados())

    def test_H11_correo_de_dominio_ignorado_tambien_se_recuerda(self):
        """Los correos de dominios ignorados (sin historial) también quedan recordados."""
        self.patch_attr(invoice_manager, "IGNORED_SENDER_DOMAINS", new={"enviomedical.com"})
        data_manager.save_processed_email_ids(set())
        self.procesar(self.correo(inline_pdf(), remitente="EnvioMedical <pedidos@enviomedical.com>",
                                  message_id="<ignorado-1@enviomedical.com>"))

        self.assertIn("ignorado-1@enviomedical.com", self.correos_recordados())

    def test_H12_correo_sin_documento_tambien_se_recuerda(self):
        """Un correo sin ningún documento útil también queda recordado."""
        data_manager.save_processed_email_ids(set())
        self.procesar(self.correo(inline_logo(2 * KB), asunto="Novedades de Carrefour",
                                  message_id="<novedades-1@carrefour.es>"))

        self.assertIn("novedades-1@carrefour.es", self.correos_recordados())

    def test_H13_correo_descartado_sin_fallos_tambien_se_recuerda(self):
        """Un documento descartado (sin fallos técnicos) también deja el correo recordado."""
        data_manager.save_processed_email_ids(set())
        self.ia_descarta("AI_NO")
        resultado = self.procesar(self.correo(inline_pdf(), message_id="<dudoso-1@x.com>"))

        self.assertEqual(resultado, 0)
        self.assertIn("dudoso-1@x.com", self.correos_recordados())

    def test_H14_sin_message_id_se_recuerda_por_uid(self):
        """Sin cabecera Message-ID, la clave del correo es 'uid:<número>'."""
        data_manager.save_processed_email_ids(set())
        self.validate.return_value = carrefour_info()
        self.procesar(self.correo(inline_pdf()), uid="77")

        self.assertIn("uid:77", self.correos_recordados())

    def test_H15_con_fallo_el_correo_no_se_recuerda(self):
        """Si un documento falla (error de la API), el correo no se recuerda para que se reintente."""
        data_manager.save_processed_email_ids(set())
        self.validate.side_effect = invoice_manager.GeminiAPIError("Límite de peticiones")
        resultado = self.procesar(self.correo(inline_pdf(), message_id="<fallo-1@x.com>"))

        self.assertEqual(resultado, 1)
        self.assertNotIn("fallo-1@x.com", self.correos_recordados())

    def test_H16_sin_archivo_de_correos_procesados_no_se_crea(self):
        """Si no existe processed_emails.json, procesar un correo no lo crea."""
        self.validate.return_value = carrefour_info()
        resultado = self.procesar(self.correo(inline_pdf(), message_id="<x-1@y.com>"))

        self.assertEqual(resultado, 0)
        self.assertFalse(os.path.exists(self.ruta_datos("processed_emails.json")))


# ── I. search_invoice_emails: línea base y no leídos ─────────────────────────

class _BuzonFalso:
    """Imitación de imaplib.IMAP4_SSL: solo lo que usa search_invoice_emails."""

    def __init__(self, no_leidos, leidos=(), cabeceras=None, leidos_fallan=None, fetch_falla=False):
        self.no_leidos = list(no_leidos)
        self.leidos = list(leidos)
        self.cabeceras = dict(cabeceras or {})   # UID -> Message-ID
        self.leidos_fallan = leidos_fallan        # None, "excepcion" o "no"
        self.fetch_falla = fetch_falla
        self.consultas = []
        self.fetches = []

    def select(self, carpeta="INBOX", readonly=False):
        return ("OK", [b"0"])

    def uid(self, comando, *args):
        if comando.upper() == "SEARCH":
            consulta = " ".join(str(a) for a in args if a is not None)
            self.consultas.append(consulta)
            if "is:read" in consulta and "newer_than:60d" in consulta:
                if self.leidos_fallan == "excepcion":
                    raise imaplib.IMAP4.error("fallo simulado en la búsqueda de correos abiertos")
                if self.leidos_fallan == "no":
                    return ("NO", [None])
                return ("OK", [_uids(self.leidos)])
            if "is:unread" in consulta:
                return ("OK", [_uids(self.no_leidos)])
            return ("NO", [None])
        if comando.upper() == "FETCH":
            self.fetches.append(args)
            if self.fetch_falla:
                return ("NO", [None])
            datos = []
            for uid in _texto(args[0]).replace(",", " ").split():
                datos.extend(_fetch_cabecera(uid, self.cabeceras.get(int(uid))))
            return ("OK", datos)
        return ("NO", [None])


class TestI_BusquedaDeCorreos(IsolatedDataTestCase):
    """I. search_invoice_emails: los abiertos sirven de línea base; los no leídos siempre se devuelven."""

    def ruta_lineas(self):
        return os.path.join(data_manager.DATA_DIR, "processed_emails.json")

    def test_I00_ventana_de_lectura_por_defecto_de_60_dias(self):
        """Los correos abiertos se miran solo en los últimos 60 días por defecto."""
        self.assertEqual(invoice_manager.READ_EMAIL_LOOKBACK_DAYS, 60)

    def test_I01_primera_ejecucion_no_devuelve_los_abiertos_y_crea_la_linea_base(self):
        """Primera vez: los correos ya abiertos no se devuelven y sus claves quedan guardadas."""
        buzon = _BuzonFalso(
            no_leidos=[1, 2, 3], leidos=[4, 5],
            cabeceras={1: "<u1@x.com>", 2: "<u2@x.com>", 3: "<u3@x.com>",
                       4: "<viejo@x.com>", 5: "<a@b.com>"})
        self.assertFalse(os.path.exists(self.ruta_lineas()))

        resultado = invoice_manager.search_invoice_emails(buzon)

        self.assertEqual(sorted(resultado), ["1", "2", "3"])
        self.assertTrue(os.path.exists(self.ruta_lineas()), "Debe crearse la línea base")
        self.assertTrue({"viejo@x.com", "a@b.com"}.issubset(data_manager.load_processed_email_ids()))
        self.assertTrue(any("is:unread" in q and "has:attachment" in q and "factura" in q
                            for q in buzon.consultas), f"Consultas: {buzon.consultas}")
        self.assertTrue(any("is:read" in q and "newer_than:60d" in q and "has:attachment" in q
                            for q in buzon.consultas), f"Consultas: {buzon.consultas}")
        self.assertTrue(buzon.fetches, "Debe pedirse la cabecera Message-ID de los correos abiertos")
        for args in buzon.fetches:
            self.assertTrue(any("BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]" in str(a) for a in args),
                            f"La cabecera se pidió sin PEEK (marcaría el correo como leído): {args}")

    def test_I02_sin_abiertos_la_linea_base_existe_igualmente(self):
        """Sin correos abiertos, la línea base se crea vacía (el archivo no se queda sin crear)."""
        buzon = _BuzonFalso(no_leidos=[1, 2], leidos=[], cabeceras={1: "<u1@x.com>", 2: "<u2@x.com>"})

        resultado = invoice_manager.search_invoice_emails(buzon)

        self.assertEqual(sorted(resultado), ["1", "2"])
        self.assertIsNotNone(data_manager.load_processed_email_ids(),
                             "Debe existir la línea base aunque esté vacía")

    def test_I03_siguientes_ejecuciones_saltan_los_abiertos_ya_vistos(self):
        """Ejecuciones posteriores: un abierto ya visto no vuelve; uno nuevo sí, sin duplicados."""
        data_manager.save_processed_email_ids({"viejo@x.com", "a@b.com"})
        buzon = _BuzonFalso(
            no_leidos=[1, 2, 3], leidos=[4, 5, 6],
            cabeceras={1: "<u1@x.com>", 2: "<u2@x.com>", 3: "<u3@x.com>",
                       4: "<viejo@x.com>", 5: "<a@b.com>", 6: "<nuevo@x.com>"})

        resultado = invoice_manager.search_invoice_emails(buzon)

        self.assertEqual(sorted(resultado), ["1", "2", "3", "6"])
        self.assertEqual(len(resultado), len(set(resultado)), f"UID repetidos: {resultado}")

    def test_I04_no_leido_se_devuelve_aunque_su_clave_ya_este_guardada(self):
        """Un correo que el usuario volvió a marcar como no leído se devuelve aunque ya estuviera guardado."""
        data_manager.save_processed_email_ids({"viejo@x.com", "a@b.com"})
        buzon = _BuzonFalso(no_leidos=[2], leidos=[4], cabeceras={2: "<a@b.com>", 4: "<viejo@x.com>"})

        resultado = invoice_manager.search_invoice_emails(buzon)

        self.assertEqual(sorted(resultado), ["2"])

    def test_I05_si_la_busqueda_de_abiertos_lanza_error_se_devuelven_los_no_leidos(self):
        """Un error al buscar los abiertos no rompe la búsqueda y no crea la línea base."""
        buzon = _BuzonFalso(no_leidos=[1, 2, 3], leidos=[4], cabeceras={4: "<viejo@x.com>"},
                            leidos_fallan="excepcion")

        resultado = invoice_manager.search_invoice_emails(buzon)

        self.assertEqual(sorted(resultado), ["1", "2", "3"])
        self.assertFalse(os.path.exists(self.ruta_lineas()),
                         "Sin búsqueda de abiertos no debe crearse la línea base")

    def test_I06_si_la_busqueda_de_abiertos_no_es_ok_se_devuelven_los_no_leidos(self):
        """Una respuesta no OK a la búsqueda de abiertos devuelve igualmente los no leídos."""
        buzon = _BuzonFalso(no_leidos=[1, 2, 3], leidos=[4], cabeceras={4: "<viejo@x.com>"},
                            leidos_fallan="no")

        resultado = invoice_manager.search_invoice_emails(buzon)

        self.assertEqual(sorted(resultado), ["1", "2", "3"])
        self.assertFalse(os.path.exists(self.ruta_lineas()),
                         "Sin búsqueda de abiertos no debe crearse la línea base")

    def test_I07_si_falla_la_cabecera_de_los_abiertos_se_devuelven_los_no_leidos(self):
        """Si FETCH de las cabeceras no es OK, se devuelven los no leídos y no se crea la línea base."""
        buzon = _BuzonFalso(no_leidos=[1, 2, 3], leidos=[4], cabeceras={4: "<viejo@x.com>"},
                            fetch_falla=True)

        resultado = invoice_manager.search_invoice_emails(buzon)

        self.assertEqual(sorted(resultado), ["1", "2", "3"])
        self.assertFalse(os.path.exists(self.ruta_lineas()),
                         "Sin cabeceras no debe crearse la línea base")


# ── J. pipeline_orchestrator: resumen y sesión vacía ─────────────────────────

class TestJ_ResumenDeRevision(IsolatedDataTestCase):
    """J. pipeline_orchestrator: resumen 'Fin de ciclo' y sesión vacía."""

    def setUp(self):
        super().setUp()
        # Estado global del orquestador: se guarda aquí y se restaura en tearDown
        self.orig_state = dict(pipeline_orchestrator._STATE)
        pipeline_orchestrator._STATE["logs"] = []
        pipeline_orchestrator._STATE["session_summary"] = []

        # Dos correos: UID 1 = pedido de Carrefour con formato rechazado; UID 2 = factura sin CIF
        self.correos = {
            "1": make_invoice_email(pdf_part(disposition="inline", filename=PEDIDO_CARREFOUR),
                                    sender=REMITENTE_CARREFOUR_ENVIOS, subject="Confirmacion de pedido"),
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
        self.patch_attr(invoice_manager, "fetch_email", side_effect=lambda mail, uid: self.correos[uid])
        self.patch_attr(invoice_manager, "validate_is_invoice", side_effect=_ia_de_dos_correos)
        self.patch_attr(invoice_manager, "process_enviomedical")
        self.patch_attr(invoice_manager, "mark_as_read")
        self.patch_attr(invoice_manager, "upload_to_drive")
        self.patch_attr(invoice_manager, "get_or_create_folder")
        self.patch_attr(invoice_manager, "get_or_create_supplier_folder")

    def tearDown(self):
        pipeline_orchestrator._STATE.clear()
        pipeline_orchestrator._STATE.update(self.orig_state)
        super().tearDown()

    def test_J01_sesion_vacia_no_muestra_los_formatos_ignorados(self):
        """Con la sesión vacía, el panel muestra el historial pero sin los documentos ignorados."""
        data_manager.add_history_entry(status="IGNORED_KNOWN_FORMAT", filename=PEDIDO_CARREFOUR,
                                       supplier="CARREFOUR", date_str="10-26",
                                       sender=REMITENTE_CARREFOUR_ENVIOS)
        data_manager.add_history_entry(status="SUCCESS", filename="factura.pdf", supplier="LIDL",
                                       date_str="10-26", sender="lidl@x.com")

        resumen = pipeline_orchestrator.get_pipeline_state()["session_summary"]

        self.assertFalse(any(e.get("status") == "IGNORED_KNOWN_FORMAT" for e in resumen),
                         "El resumen de sesión no debe incluir documentos ignorados")
        self.assertTrue(any(e.get("status") == "SUCCESS" for e in resumen),
                        "El resumen debe seguir mostrando lo archivado")

    def test_J02_fin_de_ciclo_cuenta_la_revision_como_dudosa_y_menciona_lo_ignorado(self):
        """Fin de ciclo: la revisión de datos cuenta como dudosa y los documentos ignorados aparecen."""
        data_manager.add_history_entry(status="SUCCESS", filename="previa.pdf", supplier="CARREFOUR",
                                       date_str="10-26", sender=SENDER)

        pipeline_orchestrator._execute_pipeline_worker()

        fin = [linea for linea in pipeline_orchestrator._STATE["logs"] if "Fin de ciclo" in linea]
        self.assertTrue(fin, "No hay ninguna línea 'Fin de ciclo' en los logs")
        resumen = fin[-1]
        self.assertIn("1 dudosas", resumen)
        self.assertIn("0 descartadas", resumen)
        self.assertIn("ignorado", resumen.lower())


# ── K. Dashboard (FastAPI): revisión, formatos rechazados y rescate ──────────

class TestK_PanelDeRevision(IsolatedDataTestCase):
    """K. Dashboard (FastAPI): dudosas con revisión de datos, formatos rechazados y rescate."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(app)
        self.pdf = os.path.join(self.test_data_dir, "documento.pdf")
        with open(self.pdf, "wb") as f:
            f.write(PDF_BYTES)

    def nueva_entrada(self, status, sender=REMITENTE_NUEVO, filename=PEDIDO_CARREFOUR):
        """Registro con copia del documento en cuarentena, como lo deja el flujo real."""
        return data_manager.add_history_entry(
            status=status, filename=filename, supplier="C.C. CARREFOUR, S.A.", date_str="10-26",
            original_filepath=self.pdf, sender=sender, email_subject="Confirmacion de pedido")

    def ruta_cuarentena(self, entry_id):
        entrada = data_manager.get_history_entry(entry_id)
        if not entrada or not entrada.get("quarantine_file"):
            return None
        return os.path.join(data_manager.QUARANTINE_DIR, entrada["quarantine_file"])

    def test_K01_ambiguous_incluye_dudosas_y_revision_de_datos(self):
        """GET /api/ambiguous devuelve las dudosas de fecha y las de revisión de datos, no las descartadas."""
        dudosa = data_manager.add_history_entry(status="AMBIGUOUS_DATE", filename="dudosa.pdf",
                                                supplier="GLOBAL", date_str="09-26",
                                                original_filepath=self.pdf)
        revision = self.nueva_entrada("REVIEW_MISSING_DATA")
        descartada = data_manager.add_history_entry(status="DISCARDED", filename="otra.pdf",
                                                    supplier="OTRA", date_str="10-26",
                                                    original_filepath=self.pdf)

        res = self.client.get("/api/ambiguous")

        self.assertEqual(res.status_code, 200)
        ids = {e["id"] for e in res.json()}
        self.assertIn(dudosa, ids)
        self.assertIn(revision, ids)
        self.assertNotIn(descartada, ids)

    def test_K02_rechazar_revision_aprende_el_formato_y_retira_el_archivo(self):
        """Rechazar una revisión aprende su formato, la deja como DISCARDED_CONFIRMED y borra la copia."""
        rid = self.nueva_entrada("REVIEW_MISSING_DATA")
        ruta = self.ruta_cuarentena(rid)
        self.assertIsNotNone(ruta, "REVIEW_MISSING_DATA debe guardar copia en cuarentena")
        self.assertTrue(os.path.exists(ruta))

        res = self.client.post(f"/api/review/{rid}/reject")

        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(data_manager.is_rejected_format(REMITENTE_NUEVO, "confirmacionDelPedido_99999999.pdf"),
                        "Debe quedar aprendido el formato del remitente")
        self.assertEqual(data_manager.get_history_entry(rid)["status"], "DISCARDED_CONFIRMED")
        self.assertFalse(os.path.exists(ruta), "El archivo de cuarentena debe eliminarse")

    def test_K03_rechazar_una_dudosa_da_400_y_no_cambia_nada(self):
        """Solo las revisiones de datos se rechazan así: una dudosa de fecha devuelve 400."""
        aid = self.nueva_entrada("AMBIGUOUS_DATE")
        ruta = self.ruta_cuarentena(aid)
        self.assertIsNotNone(ruta, "La dudosa debe tener copia en cuarentena")

        res = self.client.post(f"/api/review/{aid}/reject")

        self.assertEqual(res.status_code, 400)
        self.assertEqual(data_manager.get_history_entry(aid)["status"], "AMBIGUOUS_DATE")
        self.assertTrue(os.path.exists(ruta))
        self.assertEqual(data_manager.list_rejected_formats(), [])

    def test_K04_rechazar_un_id_desconocido_da_404(self):
        """Un id que no existe devuelve 404."""
        res = self.client.post("/api/review/id-que-no-existe/reject")
        self.assertEqual(res.status_code, 404)

    def test_K05_lista_los_formatos_aprendidos(self):
        """GET /api/rejected_formats muestra los formatos aprendidos."""
        registro = data_manager.learn_rejected_format(REMITENTE_NUEVO, PEDIDO_CARREFOUR)

        res = self.client.get("/api/rejected_formats")

        self.assertEqual(res.status_code, 200)
        self.assertIn(registro["id"], _ids_de_respuesta(res.json()))

    def test_K06_eliminar_un_formato_lo_olvida_y_da_404_si_no_existe(self):
        """DELETE /api/rejected_formats/{id} olvida el formato; repetirlo devuelve 404."""
        registro = data_manager.learn_rejected_format(REMITENTE_NUEVO, PEDIDO_CARREFOUR)

        res = self.client.delete(f"/api/rejected_formats/{registro['id']}")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(registro["id"], {r["id"] for r in data_manager.list_rejected_formats()})

        res_repetida = self.client.delete(f"/api/rejected_formats/{registro['id']}")
        self.assertEqual(res_repetida.status_code, 404)

    def test_K07_rescatar_una_revision_sin_crear_regla_automatica(self):
        """Rescatar sin marcar 'always_accept' sube el archivo y no crea ninguna regla por proveedor."""
        rid = self.nueva_entrada("REVIEW_MISSING_DATA")
        self.assertIsNotNone(self.ruta_cuarentena(rid), "REVIEW_MISSING_DATA debe guardar copia en cuarentena")
        self.patch_attr(invoice_manager, "authenticate_drive", return_value=MagicMock(name="drive"))
        self.patch_attr(invoice_manager, "get_or_create_folder", return_value="carpeta-id")
        self.patch_attr(invoice_manager, "get_or_create_supplier_folder", return_value="carpeta-proveedor-id")
        self.patch_attr(invoice_manager, "upload_to_drive", return_value="drive-1")

        res = self.client.post(f"/api/rescue/{rid}",
                               json={"supplier": "C.C. CARREFOUR, S.A.", "date": FECHA_ISO,
                                     "always_accept": False})

        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(data_manager.get_history_entry(rid)["status"], "SUCCESS")
        self.assertEqual(data_manager.load_vendor_rules()["rules"], {},
                         "Rescatar sin 'always_accept' no debe crear ninguna regla")


# ── L. Clave de cada correo ──────────────────────────────────────────────────

class TestL_ClaveDeCorreo(unittest.TestCase):
    """L. Clave de cada correo: su Message-ID sin corchetes, o 'uid:<n>' si no tiene cabecera."""

    def test_L01_la_clave_es_el_message_id_sin_corchetes(self):
        """Message-ID '<abc@x.com>' se guarda como 'abc@x.com', también con espacios alrededor."""
        self.assertEqual(invoice_manager.email_key("<abc@x.com>", 7), "abc@x.com")
        self.assertEqual(invoice_manager.email_key("  <abc@x.com>  ", 7), "abc@x.com")

    def test_L02_sin_message_id_la_clave_es_el_uid(self):
        """Sin cabecera Message-ID, la clave es 'uid:<número>'."""
        self.assertEqual(invoice_manager.email_key(None, 7), "uid:7")
        self.assertEqual(invoice_manager.email_key("", 7), "uid:7")


# ── M. Hallazgos de la revisión independiente ────────────────────────────────

class TestM_RevisionIndependiente(IsolatedDataTestCase):
    """M. Casos encontrados por la revisión independiente."""

    def test_M1_nombres_reservados_de_windows_se_guardan(self):
        """PRN.pdf, AUX.pdf, COM1.pdf o LPT1.pdf no deben romper el correo entero."""
        tmp = tempfile.mkdtemp(prefix="test_reservados_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name in ("PRN.pdf", "aux.pdf", "COM1.pdf", "LPT1.pdf", "NUL.pdf"):
            msg = make_invoice_email(pdf_part(disposition="attachment", filename=name))
            saved = invoice_manager.extract_attachments(msg, tmp)
            self.assertEqual(len(saved), 1, name)
            self.assertTrue(os.path.isfile(saved[0]), name)

    def test_M1b_un_nombre_normal_no_cambia(self):
        self.assertEqual(invoice_manager._safe_attachment_name("PRNfactura.pdf"), "PRNfactura.pdf")

    def _add(self, status, name="doc.pdf"):
        return data_manager.add_history_entry(status=status, filename=name, supplier="X", date_str="11-26")

    def test_M2_lo_ignorado_no_expulsa_lo_pendiente_de_revisar(self):
        """El tope de 500 no debe hacer desaparecer un documento pendiente."""
        pending = [self._add("REVIEW_MISSING_DATA"), self._add("AMBIGUOUS_DATE"), self._add("DISCARDED")]
        for _ in range(510):
            self._add("IGNORED_KNOWN_FORMAT")
        history = data_manager.get_history(limit=1000)
        self.assertLessEqual(len(history), 500)
        ids = {e["id"] for e in history}
        for item in pending:
            self.assertIn(item, ids)

    def test_M2b_primero_se_retira_lo_ignorado(self):
        """Antes de retirar una factura archivada se retiran los documentos ignorados."""
        success = self._add("SUCCESS")
        for _ in range(499):
            self._add("IGNORED_KNOWN_FORMAT")
        self._add("SUCCESS", "nueva.pdf")
        ids = {e["id"] for e in data_manager.get_history(limit=1000)}
        self.assertIn(success, ids)

    def test_M4_un_nombre_solo_numerico_no_aprende_un_formato_general(self):
        """Rechazar '111.pdf' no debe ocultar otra factura '222.pdf' del mismo remitente."""
        data_manager.learn_rejected_format("a@b.com", "111.pdf")
        self.assertTrue(data_manager.is_rejected_format("a@b.com", "111.pdf"))
        self.assertFalse(data_manager.is_rejected_format("a@b.com", "222.pdf"))

    def test_M4b_el_numero_pegado_a_una_palabra_conserva_la_palabra(self):
        """Rechazar 'factura2.pdf' no debe ocultar '2026.pdf', pero sí 'factura3.pdf'."""
        data_manager.learn_rejected_format("a@b.com", "factura2.pdf")
        self.assertFalse(data_manager.is_rejected_format("a@b.com", "2026.pdf"))
        self.assertTrue(data_manager.is_rejected_format("a@b.com", "factura3.pdf"))

    def test_M6_archivar_conserva_el_nombre_original_en_drive(self):
        """Lo que se sube a Drive se llama como el documento, no como su copia interna."""
        source = os.path.join(self.test_data_dir, "13037232.pdf")
        with open(source, "wb") as f:
            f.write(b"%PDF-1.4 prueba")
        item = data_manager.add_history_entry(
            status="REVIEW_MISSING_DATA", filename="13037232.pdf", supplier="CARREFOUR",
            date_str="11-26", original_filepath=source)
        uploaded = []
        def fake_upload(service, path, folder):
            uploaded.append(os.path.basename(path))
            self.assertTrue(os.path.exists(path))
            return "id-drive"
        self.patch_attr(invoice_manager, "authenticate_drive")
        self.patch_attr(invoice_manager, "get_or_create_folder", return_value="c")
        self.patch_attr(invoice_manager, "get_or_create_supplier_folder", return_value="p")
        self.patch_attr(invoice_manager, "upload_to_drive", side_effect=fake_upload)
        res = TestClient(app).post(f"/api/rescue/{item}", json={
            "supplier": "CARREFOUR", "date": "2026-11-06", "always_accept": False})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(uploaded, ["13037232.pdf"])


if __name__ == "__main__":
    unittest.main()
