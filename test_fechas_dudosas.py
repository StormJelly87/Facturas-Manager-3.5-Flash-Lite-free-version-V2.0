"""
test_fechas_dudosas.py — Cuándo debe preguntarse una fecha y qué pasa al descartar una dudosa.

N. No se pregunta si el mes va con letras o si la empresa es española y el texto
   confirma día/mes. Se sigue preguntando en todos los casos en que podría acertarse mal.
P. Descartar una factura con fecha dudosa retira su copia provisional de Drive.

Sin red: la IA y Drive son simulados y los datos van a una carpeta temporal.
"""

import os
import tempfile
import shutil
import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import data_manager
import invoice_manager
from dashboard.app import app
from test_email_attachments import IsolatedDataTestCase, PDF_BYTES, SENDER

VALIDATE_REAL = invoice_manager.validate_is_invoice
CIF_ESPANOL = "A28425270"
CIF_PROPIO = "B11111119"
VAT_ALEMAN = "DE123456789"
ENTIDAD_EXTRANJERA = "N0057922G"


class TestN_CuandoSePreguntaLaFecha(IsolatedDataTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="test_fechas_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.patch_attr(invoice_manager, "TAX_ID_SUPPLIER_MAP", new={})
        self.patch_attr(invoice_manager, "SUPPLIER_ALIASES", new={})
        self.patch_attr(invoice_manager, "OWN_TAX_IDS", new={CIF_PROPIO})

    def validar(self, tax_id, texto, fecha_ia="2026-10-05", tipo_fecha=None, **extra):
        pdf = os.path.join(self.tmp, "doc.pdf")
        with open(pdf, "wb") as f:
            f.write(PDF_BYTES)
        plumber = MagicMock(name="pdfplumber")
        pagina = MagicMock(name="pagina")
        pagina.extract_text.return_value = texto
        plumber.open.return_value.__enter__.return_value.pages = [pagina]
        self.patch_attr(invoice_manager, "pdfplumber", new=plumber)
        modelo = MagicMock(name="modelo")
        modelo.generate_content.return_value = MagicMock(
            text=f'{{"is_invoice":"SI","supplier":"Empresa SL","tax_id":"{tax_id}","date":"{fecha_ia}"'
                 + (f',"date_kind":"{tipo_fecha}"' if tipo_fecha else "") + "}")
        return VALIDATE_REAL(modelo, pdf, sender_email=SENDER, reference_fps=[], **extra)

    # ── La IA elige una fecha que no es la de la factura ──

    def test_S01_la_etiqueta_de_factura_gana_si_la_ia_elige_el_vencimiento(self):
        info = self.validar(CIF_ESPANOL, "Factura. Fecha: 05.06.2026. Vencimiento: 10.07.2026", fecha_ia="2026-07-10")
        self.assertEqual(info["date"], "06-26")
        self.assertFalse(info["is_ambiguous"])

    def test_S02_etiquetas_habituales_de_factura(self):
        for texto in ("Fecha de factura: 15/04/2026. Vencimiento: 20/05/2026",
                      "Fecha de emisión 15/04/2026 Vencimiento 20/05/2026",
                      "Invoice date: 15-Apr-2026. Due date: 20-May-2026",
                      "Rechnungsdatum 15.04.2026 Fällig 20.05.2026"):
            self.assertEqual(self.validar(CIF_ESPANOL, texto, fecha_ia="2026-05-20")["date"], "04-26", texto)

    def test_S03_due_date_no_cuenta_como_fecha_de_factura(self):
        info = self.validar(CIF_ESPANOL, "Due date: 20/05/2026 Ref 15/04/2026", fecha_ia="2026-04-15")
        self.assertEqual(info["date"], "04-26")

    def test_S04_sin_etiqueta_de_factura_y_la_ia_elige_el_vencimiento_pregunta(self):
        """No se sabe cuál es la buena: decide el usuario, también con día mayor de 12."""
        info = self.validar(CIF_ESPANOL, "Doc 15/04/2026. Vencimiento: 20/05/2026", fecha_ia="2026-05-20")
        self.assertTrue(info["is_ambiguous"])
        self.assertTrue(info["ambiguous_options"], "Debe ofrecer al menos una opción")

    def test_S05_vencimiento_con_regla_aprendida_tambien_pregunta(self):
        data_manager.save_vendor_rule("Empresa SL", date_format="DD/MM/YYYY")
        info = self.validar(CIF_ESPANOL, "Doc 15/04/2026. Vencimiento: 20/05/2026", fecha_ia="2026-05-20")
        self.assertTrue(info["is_ambiguous"])

    def test_S06_fecha_posterior_al_correo_pregunta(self):
        """Una factura no puede estar emitida después de haberla recibido."""
        from datetime import datetime
        info = self.validar(CIF_ESPANOL, "Doc 20/05/2026", fecha_ia="2026-05-20", email_date=datetime(2026, 4, 16))
        self.assertTrue(info["is_ambiguous"])

    def test_S07_fecha_el_mismo_dia_o_anterior_al_correo_no_pregunta(self):
        from datetime import datetime
        for correo in (datetime(2026, 5, 20), datetime(2026, 5, 19), datetime(2026, 6, 30)):
            info = self.validar(CIF_ESPANOL, "Fecha 20/05/2026", fecha_ia="2026-05-20", email_date=correo)
            self.assertFalse(info["is_ambiguous"], correo)

    def test_S08_process_invoice_pasa_la_fecha_del_correo(self):
        from datetime import datetime
        from test_email_attachments import make_invoice_email, inline_pdf, carrefour_info
        msg = make_invoice_email(inline_pdf())
        msg["Date"] = "Fri, 06 Nov 2026 10:00:00 +0100"
        self.patch_attr(invoice_manager, "fetch_email", return_value=msg)
        validate = self.patch_attr(invoice_manager, "validate_is_invoice", return_value=carrefour_info())
        self.patch_attr(invoice_manager, "mark_as_read")
        self.patch_attr(invoice_manager, "upload_to_drive", return_value="id")
        self.patch_attr(invoice_manager, "get_or_create_folder", return_value="c")
        self.patch_attr(invoice_manager, "get_or_create_supplier_folder", return_value="p")
        invoice_manager.process_invoice(MagicMock(), "1", MagicMock(), MagicMock(), self.tmp)
        self.assertEqual(validate.call_args.kwargs["email_date"], datetime(2026, 11, 6, 10, 0))

    def test_S09_correo_sin_fecha_valida_no_falla(self):
        self.assertIsNone(invoice_manager._email_datetime("no es una fecha"))
        self.assertIsNone(invoice_manager._email_datetime(None))

    # ── Tercera revisión: el programa no sustituye en silencio una fecha bien leída ──

    def test_U01_tabla_la_columna_vecina_no_gana_a_la_ia(self):
        info = self.validar(VAT_ALEMAN, "Order date Invoice date\n20/03/2026 15/04/2026", fecha_ia="2026-04-15")
        self.assertEqual(info["date"], "04-26")
        self.assertFalse(info["is_ambiguous"])

    def test_U02_tabla_con_vencimiento_no_pregunta_de_mas(self):
        for texto in ("Fecha de factura Vencimiento\n15/04/2026 20/05/2026",
                      "Invoice date Due date\n15/04/2026 20/05/2026"):
            info = self.validar(CIF_ESPANOL, texto, fecha_ia="2026-04-15")
            self.assertEqual(info["date"], "04-26", texto)
            self.assertFalse(info["is_ambiguous"], texto)

    def test_U03_fechas_con_letras_en_otros_idiomas_confirman_a_la_ia(self):
        for texto in ("Date d'émission : 15 avril 2026. Échéance : 20/05/2026",
                      "Data emissione: 15 aprile 2026. Scadenza: 20/05/2026",
                      "Rechnungsdatum: 15. April 2026. Fällig: 20/05/2026",
                      "Data de emissao: 15 de abril de 2026. Vencimento: 20/05/2026"):
            info = self.validar("FR12345678901", texto, fecha_ia="2026-04-15")
            self.assertEqual(info["date"], "04-26", texto)
            self.assertFalse(info["is_ambiguous"], texto)

    def test_U04_varias_fechas_y_ninguna_es_la_de_la_ia_pregunta_sin_cambiarla(self):
        info = self.validar(CIF_ESPANOL, "Doc 15/04/2026. Otro 20/05/2026", fecha_ia="2026-06-20")
        self.assertEqual(info["date"], "06-26")
        self.assertTrue(info["is_ambiguous"])

    def test_U05_una_sola_fecha_en_el_texto_corrige_a_la_ia(self):
        self.assertEqual(self.validar(CIF_ESPANOL, "Doc 15/04/2026", fecha_ia="2026-06-20")["date"], "04-26")

    def test_U06_la_ia_avisa_de_que_es_un_vencimiento(self):
        """Cubre cualquier idioma y tabla: 'Fecha de cobro', 'Scadenza', 'Échéance'..."""
        for tipo in ("VENCIMIENTO", "OTRA"):
            info = self.validar(CIF_ESPANOL, "Emision: 15/04/2026. Fecha de cobro: 20/05/2026",
                                fecha_ia="2026-05-20", tipo_fecha=tipo)
            self.assertTrue(info["is_ambiguous"], tipo)

    def test_U07_la_ia_dice_emision_no_pregunta(self):
        info = self.validar(CIF_ESPANOL, "Emision: 15/04/2026. Cobro: 20/05/2026",
                            fecha_ia="2026-04-15", tipo_fecha="EMISION")
        self.assertFalse(info["is_ambiguous"])

    def test_U08_dos_etiquetas_de_factura_distintas_preguntan(self):
        info = self.validar(CIF_ESPANOL, "Fecha de la factura: 15/04/2026. Fecha de emisión: 20/05/2026",
                            fecha_ia="2026-05-20")
        self.assertTrue(info["is_ambiguous"])

    def test_U09_fecha_en_numeros_y_en_letras_es_cierta(self):
        info = self.validar(VAT_ALEMAN, "Invoice date 05/04/2026 (5 de abril de 2026)", fecha_ia="2026-04-05")
        self.assertFalse(info["is_ambiguous"])

    def test_U10_29_de_febrero_sin_año_no_descarta(self):
        info = self.validar(CIF_ESPANOL, "Fecha de factura: 29 de febrero. Vencimiento: 15/03/2026", fecha_ia="2026-03-15")
        self.assertIsNotNone(info)

    def test_U11_fecha_de_la_ia_con_zona_horaria_no_descarta(self):
        info = self.validar(CIF_ESPANOL, "Fecha: 15/04/2026", fecha_ia="2026-04-15T00:00:00Z")
        self.assertEqual(info["date"], "04-26")

    def test_U12_factura_en_otro_idioma_con_mes_en_letras_no_se_descarta(self):
        for texto in ("Facture n. 2026-0412 du 15 avril 2026", "Fattura n. 12 del 15 aprile 2026",
                      "Rechnungsdatum: 5. Oktober 2026"):
            self.assertIsNotNone(self.validar("FR12345678901", texto, fecha_ia="2026-04-15"), texto)

    def test_U13_el_prompt_pide_la_fecha_de_emision_y_su_tipo(self):
        modelo_texto = open(invoice_manager.__file__, encoding="utf-8").read()
        self.assertIn('"date_kind"', modelo_texto)
        self.assertIn("nunca la de vencimiento", modelo_texto)

    # ── Una palabra suelta no fija el mes ──

    def test_T01_may_suelto_no_cambia_el_mes_de_la_ia(self):
        self.assertEqual(self.validar(VAT_ALEMAN, "You may pay by transfer", fecha_ia="2026-10-20")["date"], "10-26")

    def test_T02_mar_suelto_no_cambia_el_mes_de_la_ia(self):
        self.assertEqual(self.validar(CIF_ESPANOL, "Hotel vista al mar", fecha_ia="2026-10-20")["date"], "10-26")

    def test_T03_mes_y_año_juntos_siguen_valiendo(self):
        self.assertEqual(self.validar(CIF_ESPANOL, "Factura de marzo 2026", fecha_ia="2026-10-20")["date"], "03-26")

    def test_T04_fecha_americana_con_letras_es_cierta(self):
        info = self.validar(VAT_ALEMAN, "Invoice date: November 6, 2026", fecha_ia="2026-11-06")
        self.assertEqual(info["date"], "11-26")
        self.assertFalse(info["is_ambiguous"])

    # ── Casos en los que ya no debe preguntar ──

    def test_N01_cif_espanol_y_fecha_numerica(self):
        info = self.validar(CIF_ESPANOL, "Factura. Fecha 05/10/2026")
        self.assertFalse(info["is_ambiguous"])
        self.assertEqual(info["date"], "10-26")

    def test_N02_cif_espanol_con_prefijo_es(self):
        self.assertFalse(self.validar("ES" + CIF_ESPANOL, "Fecha 05/10/2026")["is_ambiguous"])

    def test_N03_mes_con_letras_aunque_sea_extranjera(self):
        info = self.validar(VAT_ALEMAN, "Fecha 5 de octubre de 2026")
        self.assertFalse(info["is_ambiguous"])
        self.assertEqual(info["date"], "10-26")

    def test_N04_mes_abreviado_aunque_sea_extranjera(self):
        info = self.validar(VAT_ALEMAN, "Invoice date 05-Oct-2026")
        self.assertFalse(info["is_ambiguous"])
        self.assertEqual(info["date"], "10-26")

    def test_N05_cif_espanol_con_fecha_con_puntos(self):
        info = self.validar(CIF_ESPANOL, "Fecha 05.10.2026")
        self.assertFalse(info["is_ambiguous"])
        self.assertEqual(info["date"], "10-26")

    # ── Casos que se nos podían escapar: debe seguir preguntando ──

    def test_N10_extranjera_con_fecha_numerica(self):
        self.assertTrue(self.validar(VAT_ALEMAN, "Invoice date 05/10/2026")["is_ambiguous"])

    def test_N11_el_cif_leido_es_el_de_la_propia_empresa(self):
        """La IA puede devolver el CIF del cliente: no demuestra que el emisor sea español."""
        self.assertTrue(self.validar(CIF_PROPIO, "Invoice date 05/10/2026")["is_ambiguous"])
        self.assertTrue(self.validar("ES" + CIF_PROPIO, "Invoice date 05/10/2026")["is_ambiguous"])

    def test_N12_entidad_extranjera_con_nif_espanol(self):
        self.assertTrue(self.validar(ENTIDAD_EXTRANJERA, "Invoice date 05/10/2026")["is_ambiguous"])

    def test_N13_empresa_espanola_con_programa_americano(self):
        """Otra fecha del documento, 10/30/2026, delata que escribe primero el mes."""
        info = self.validar(CIF_ESPANOL, "Date 05/10/2026 Due date 10/30/2026")
        self.assertTrue(info["is_ambiguous"])

    def test_N14_documento_escaneado_sin_texto(self):
        """Sin texto, la fecha solo la ha leído la IA: no hay nada que la confirme."""
        self.assertTrue(self.validar(CIF_ESPANOL, "")["is_ambiguous"])

    def test_N15_la_ia_lee_al_reves_lo_que_dice_el_texto(self):
        """El texto dice 05.10 y la IA dice 10 de mayo: no se da por buena."""
        info = self.validar(CIF_ESPANOL, "Doc 05.10.2026", fecha_ia="2026-05-10")
        self.assertTrue(info["is_ambiguous"])

    def test_N16_una_palabra_suelta_no_es_un_mes_con_letras(self):
        """'may' suelto en un texto en inglés no fija la fecha sin un día delante."""
        info = self.validar(VAT_ALEMAN, "You may pay by transfer", fecha_ia="2026-10-06")
        self.assertTrue(info["is_ambiguous"])

    def test_N17_palabra_suelta_con_cif_espanol(self):
        info = self.validar(CIF_ESPANOL, "Se ruega mar adentro", fecha_ia="2026-10-05")
        self.assertTrue(info["is_ambiguous"])

    # ── Lo ya aprendido manda ──

    def test_N20_regla_americana_aprendida_gana_al_cif_espanol(self):
        data_manager.save_vendor_rule("Empresa SL", date_format="MM/DD/YYYY")
        info = self.validar(CIF_ESPANOL, "Fecha 05/10/2026")
        self.assertFalse(info["is_ambiguous"])
        self.assertEqual(info["date"], "05-26")

    def test_N21_regla_americana_no_invierte_un_mes_con_letras(self):
        """'5 de octubre' es octubre, sea cual sea el formato aprendido."""
        data_manager.save_vendor_rule("Empresa SL", date_format="MM/DD/YYYY")
        info = self.validar(VAT_ALEMAN, "Fecha 5 de octubre de 2026")
        self.assertEqual(info["date"], "10-26")

    def test_N22_fecha_futura_con_letras_no_se_invierte(self):
        """Un mes escrito no se cambia por el día: 5 de diciembre de 2099 no es 12 de mayo."""
        info = self.validar(VAT_ALEMAN, "Fecha 5 de diciembre de 2099", fecha_ia="2099-12-05")
        self.assertNotEqual(info["date"], "05-99")

    # ── Identificador español ──

    def test_N30_que_cuenta_como_empresa_espanola(self):
        for valido in (CIF_ESPANOL, "ESA28425270", "A-28425270", "12345678Z", "X1234567L", "ESB12345674",
                       "A11111119", "G11111119"):
            self.assertTrue(invoice_manager._is_spanish_tax_id(valido), valido)
        for no_valido in (VAT_ALEMAN, ENTIDAD_EXTRANJERA, "W1234567A", "PT123456789", "123456789", "DESCONOCIDO",
                          "RESCATADO-REF", "ACEPTADO-POR-REGLA", CIF_PROPIO, "ES" + CIF_PROPIO, "B-11111119", "", None,
                          "12345678K", "12.345.678-K", "V12345678", "G12345678"):
            self.assertFalse(invoice_manager._is_spanish_tax_id(no_valido), no_valido)

    # ── Otras fechas del documento (hallazgos de la revisión independiente) ──

    def test_Q01_el_periodo_no_gana_a_la_fecha_de_la_factura(self):
        texto = "FACTURA. Periodo de facturacion: 01/04/2026 - 30/04/2026. Fecha de emision: 02/05/2026."
        info = self.validar(CIF_ESPANOL, texto, fecha_ia="2026-05-02")
        self.assertEqual(info["date"], "05-26")
        self.assertFalse(info["is_ambiguous"])

    def test_Q02_el_pedido_y_la_entrega_no_ganan(self):
        self.assertEqual(self.validar(CIF_ESPANOL, "Pedido 12/02/2026. Fecha de emision: 03/04/2026",
                                      fecha_ia="2026-04-03")["date"], "04-26")
        self.assertEqual(self.validar(VAT_ALEMAN, "Lieferdatum 20/04/2026. Rechnungsdatum 15/05/2026",
                                      fecha_ia="2026-05-15")["date"], "05-26")

    def test_Q03_el_vencimiento_con_letras_no_gana(self):
        self.assertEqual(self.validar(CIF_ESPANOL, "Vencimiento: 15 de mayo de 2026. Fecha de factura: 3 de abril de 2026",
                                      fecha_ia="2026-04-03")["date"], "04-26")
        self.assertEqual(self.validar(VAT_ALEMAN, "Payment due 15-May-2026. Invoice date 3-Apr-2026",
                                      fecha_ia="2026-04-03")["date"], "04-26")

    def test_Q04_vencimiento_con_letras_y_fecha_con_puntos(self):
        info = self.validar(CIF_ESPANOL, "Factura. Fecha: 03.04.2026. Vencimiento: 15 de mayo de 2026",
                            fecha_ia="2026-04-03")
        self.assertEqual(info["date"], "04-26")

    def test_Q05_una_fecha_imposible_no_anula_las_siguientes(self):
        info = self.validar(CIF_ESPANOL, "Factura. Ref 31/02/2026. Fecha 15/04/2026", fecha_ia="2020-01-20")
        self.assertEqual(info["date"], "04-26")

    def test_Q06_la_ia_invierte_la_fecha_con_otras_delante(self):
        """El texto corrige la lectura de la IA aunque esa fecha no sea la primera."""
        info = self.validar(CIF_ESPANOL, "Pedido 12/02/2026. Fecha 05/10/2026", fecha_ia="2026-05-10")
        self.assertEqual(info["date"], "10-26")

    def test_Q07_mes_con_letras_contra_la_ia_con_varias_fechas_pregunta(self):
        """Dos fechas con letras y ninguna coincide con la IA: no se da por cierta."""
        info = self.validar(VAT_ALEMAN, "Doc 5 de octubre de 2026. Otro 9 de noviembre de 2026",
                            fecha_ia="2026-03-04")
        self.assertTrue(info["is_ambiguous"])

    def test_Q08_mes_con_letras_unico_sigue_mandando_sobre_la_ia(self):
        info = self.validar(VAT_ALEMAN, "Fecha 5 de octubre de 2026", fecha_ia="2026-03-04")
        self.assertEqual(info["date"], "10-26")
        self.assertFalse(info["is_ambiguous"])

    def test_Q10_identificador_extranjero_parecido_al_espanol_pregunta(self):
        texto = "Invoice date 03/04/2026"
        for tax_id in ("12345678K", "V12345678"):
            self.assertTrue(self.validar(tax_id, texto, fecha_ia="2026-04-03")["is_ambiguous"], tax_id)


class TestR_BotonesDelPanel(unittest.TestCase):

    def test_R1_los_botones_no_se_rompen_con_un_apostrofo(self):
        """L'OREAL entre comillas simples dentro de onclick deja el botón muerto."""
        ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard", "static", "index.html")
        with open(ruta, encoding="utf-8") as f:
            html = f.read()
        self.assertNotIn("'${escapeHtml(", html)

    def test_R2_el_ejemplo_documenta_el_cif_propio(self):
        import json
        ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "supplier_aliases.example.json")
        with open(ruta, encoding="utf-8") as f:
            self.assertIn("own_tax_ids", json.load(f))


class TestP_DescartarFechaDudosa(IsolatedDataTestCase):

    def setUp(self):
        super().setUp()
        self.client = TestClient(app)
        self.drive = self.patch_attr(invoice_manager, "authenticate_drive")
        self.trash = self.patch_attr(invoice_manager, "trash_drive_file", return_value=True)

    def _dudosa(self, drive_file_id="id-drive", status="AMBIGUOUS_DATE"):
        fuente = os.path.join(self.test_data_dir, "13.pdf")
        with open(fuente, "wb") as f:
            f.write(PDF_BYTES)
        return data_manager.add_history_entry(
            status=status, filename="13.pdf", supplier="CARREFOUR", date_str="11-26",
            drive_file_id=drive_file_id, drive_folder_id="carpeta" if drive_file_id else None,
            drive_folder_path="2026 / 11-26 / CARREFOUR" if drive_file_id else None,
            original_filepath=fuente)

    def test_P1_retira_de_drive_y_conserva_la_copia_para_rescatar(self):
        item = self._dudosa()
        res = self.client.post(f"/api/discard_ambiguous/{item}")
        self.assertEqual(res.status_code, 200)
        self.trash.assert_called_once_with(self.drive.return_value, "id-drive")
        entrada = data_manager.get_history_entry(item)
        self.assertEqual(entrada["status"], "DISCARDED")
        self.assertIsNone(entrada["drive_file_id"])
        self.assertIsNone(entrada["drive_folder_id"])
        self.assertIsNone(entrada["drive_folder_path"])
        self.assertIsNotNone(data_manager.get_quarantine_path(item))

    def test_P2_si_drive_falla_no_cambia_nada(self):
        self.trash.return_value = False
        item = self._dudosa()
        res = self.client.post(f"/api/discard_ambiguous/{item}")
        self.assertEqual(res.status_code, 500)
        entrada = data_manager.get_history_entry(item)
        self.assertEqual(entrada["status"], "AMBIGUOUS_DATE")
        self.assertEqual(entrada["drive_file_id"], "id-drive")

    def test_P3_sin_archivo_en_drive_no_llama_a_drive(self):
        item = self._dudosa(drive_file_id=None)
        self.assertEqual(self.client.post(f"/api/discard_ambiguous/{item}").status_code, 200)
        self.trash.assert_not_called()
        self.assertEqual(data_manager.get_history_entry(item)["status"], "DISCARDED")

    def test_P4_no_retira_una_factura_ya_archivada(self):
        """Esta acción solo vale para dudosas: una factura archivada no se toca."""
        item = self._dudosa(status="SUCCESS")
        self.assertEqual(self.client.post(f"/api/discard_ambiguous/{item}").status_code, 400)
        self.trash.assert_not_called()
        self.assertEqual(data_manager.get_history_entry(item)["status"], "SUCCESS")

    def test_P5_id_desconocido(self):
        self.assertEqual(self.client.post("/api/discard_ambiguous/nada").status_code, 404)


class TestP2_PapeleraDeDrive(IsolatedDataTestCase):

    def test_va_a_la_papelera_y_no_se_borra_para_siempre(self):
        servicio = MagicMock(name="drive")
        self.assertTrue(invoice_manager.trash_drive_file(servicio, "x"))
        kwargs = servicio.files.return_value.update.call_args.kwargs
        self.assertEqual(kwargs["fileId"], "x")
        self.assertEqual(kwargs["body"], {"trashed": True})
        servicio.files.return_value.delete.assert_not_called()

    def _error(self, status):
        error = Exception("fallo")
        error.resp = MagicMock(status=status)
        servicio = MagicMock(name="drive")
        servicio.files.return_value.update.return_value.execute.side_effect = error
        return servicio

    def test_si_ya_no_existe_cuenta_como_retirado(self):
        self.assertTrue(invoice_manager.trash_drive_file(self._error(404), "x"))

    def test_otro_error_avisa(self):
        self.assertFalse(invoice_manager.trash_drive_file(self._error(500), "x"))


if __name__ == "__main__":
    unittest.main()
