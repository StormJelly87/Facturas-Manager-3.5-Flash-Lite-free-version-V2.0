"""
test_dashboard_and_rules.py — Suite de pruebas automatizadas aisladas.
"""

import os
import shutil
import tempfile
import unittest
from fastapi.testclient import TestClient

import data_manager
from dashboard.app import app


class TestDashboardAndRules(unittest.TestCase):

    def setUp(self):
        # 1. Crear entorno temporal aislado para no tocar los datos del usuario
        self.test_data_dir = tempfile.mkdtemp(prefix="test_data_suite_")
        self.orig_data_dir = data_manager.DATA_DIR
        self.orig_history = data_manager.HISTORY_FILE
        self.orig_rules = data_manager.RULES_FILE
        self.orig_schedule = data_manager.SCHEDULE_FILE
        self.orig_quarantine = data_manager.QUARANTINE_DIR

        data_manager.DATA_DIR = self.test_data_dir
        data_manager.QUARANTINE_DIR = os.path.join(self.test_data_dir, "quarantine")
        data_manager.HISTORY_FILE = os.path.join(self.test_data_dir, "invoices_history.json")
        data_manager.RULES_FILE = os.path.join(self.test_data_dir, "vendor_rules.json")
        data_manager.SCHEDULE_FILE = os.path.join(self.test_data_dir, "schedule_config.json")
        data_manager._ensure_dirs()

        # Crear dummy PDF
        self.dummy_pdf = os.path.join(self.test_data_dir, "dummy_factura.pdf")
        with open(self.dummy_pdf, "wb") as f:
            f.write(b"%PDF-1.4 Dummy invoice content for isolated testing")

        self.client = TestClient(app)

    def tearDown(self):
        # Restaurar rutas originales y limpiar carpeta temporal
        data_manager.DATA_DIR = self.orig_data_dir
        data_manager.QUARANTINE_DIR = self.orig_quarantine
        data_manager.HISTORY_FILE = self.orig_history
        data_manager.RULES_FILE = self.orig_rules
        data_manager.SCHEDULE_FILE = self.orig_schedule
        shutil.rmtree(self.test_data_dir, ignore_errors=True)

    def test_data_manager_history_and_quarantine(self):
        """Verifica que el historial y la cuarentena registren correctamente de forma aislada."""
        entry_id = data_manager.add_history_entry(
            status="DISCARDED",
            filename="albaran_aislado.pdf",
            supplier="PROVEEDOR AISLADO",
            date_str="06-26",
            reason="Sin CIF detectable",
            original_filepath=self.dummy_pdf,
            email_subject="Entrega pedido",
            sender="albaranes@aislado.com",
        )
        self.assertTrue(entry_id)

        entry = data_manager.get_history_entry(entry_id)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "DISCARDED")
        self.assertEqual(entry["supplier"], "PROVEEDOR AISLADO")

        q_path = data_manager.get_quarantine_path(entry_id)
        self.assertIsNotNone(q_path)
        self.assertTrue(os.path.exists(q_path))

    def test_vendor_rules_learning_detailed(self):
        """Verifica que las reglas por proveedor se memoricen con explicación detallada."""
        rule = data_manager.save_vendor_rule(
            supplier_name="ACME SUPPLIES S.L.",
            date_format="DD/MM/YYYY",
            always_accept=True,
            learned_from="rescate_manual",
            origin_document="factura_acme.pdf",
            user_reason="Es un ticket con CIF al pie",
        )
        self.assertEqual(rule["date_format"], "DD/MM/YYYY")
        self.assertTrue(rule["always_accept"])
        self.assertIn("rescatar manualmente", rule.get("human_explanation", ""))

        matched = data_manager.get_vendor_rule("Acme Supplies")
        self.assertIsNotNone(matched)
        self.assertEqual(matched["date_format"], "DD/MM/YYYY")

    def test_ambiguity_detection_and_discard(self):
        """Verifica el descarte de una factura dudosa."""
        entry_id = data_manager.add_history_entry(
            status="AMBIGUOUS_DATE",
            filename="factura_dudosa_test.pdf",
            supplier="GLOBAL TOOLS TEST",
            date_str="09-26",
            drive_file_id="drive_mock_12345",
            drive_folder_id="folder_mock_999",
            original_filepath=self.dummy_pdf,
            detected_dates=[
                {"day": 9, "month": 6, "year": 2026, "format": "DD/MM/YYYY", "label": "9 de Junio de 2026"},
                {"day": 6, "month": 9, "year": 2026, "format": "MM/DD/YYYY", "label": "6 de Septiembre de 2026"},
            ]
        )
        entry = data_manager.get_history_entry(entry_id)
        self.assertEqual(entry["status"], "AMBIGUOUS_DATE")

        # Descartar
        ok = data_manager.discard_history_entry(entry_id, "Descartado en prueba")
        self.assertTrue(ok)
        updated = data_manager.get_history_entry(entry_id)
        self.assertEqual(updated["status"], "DISCARDED")
        self.assertEqual(updated["reason"], "Descartado en prueba")

    def test_fastapi_endpoints(self):
        """Prueba los endpoints REST."""
        res = self.client.get("/api/status")
        self.assertEqual(res.status_code, 200)

        res = self.client.get("/api/history")
        self.assertEqual(res.status_code, 200)

        res = self.client.get("/api/schedule")
        self.assertEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
