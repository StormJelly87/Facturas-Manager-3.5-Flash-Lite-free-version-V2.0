"""
test_dashboard_and_rules.py — Suite de pruebas automatizadas para el Dashboard,
la persistencia de datos, la cuarentena, las reglas aprendidas y las APIs de FastAPI.
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime
from fastapi.testclient import TestClient

import data_manager
from dashboard.app import app


class TestDashboardAndRules(unittest.TestCase):

    def setUp(self):
        # Crear un archivo temporal de prueba para simular un PDF
        self.tmp_dir = tempfile.mkdtemp(prefix="test_invoice_suite_")
        self.dummy_pdf = os.path.join(self.tmp_dir, "factura_test.pdf")
        with open(self.dummy_pdf, "wb") as f:
            f.write(b"%PDF-1.4 Dummy invoice content for testing")

        self.client = TestClient(app)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_data_manager_history_and_quarantine(self):
        """Verifica que el historial y la cuarentena registren correctamente."""
        # 1. Registrar entrada descartada
        entry_id = data_manager.add_history_entry(
            status="DISCARDED",
            filename="albaran_suelto.pdf",
            supplier="PROVEEDOR TEST",
            date_str="06-26",
            reason="Sin CIF detectable",
            original_filepath=self.dummy_pdf,
            email_subject="Entrega pedido",
            sender="albaranes@proveedor.com",
        )
        self.assertTrue(entry_id)

        # 2. Verificar que se puede recuperar
        entry = data_manager.get_history_entry(entry_id)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "DISCARDED")
        self.assertEqual(entry["supplier"], "PROVEEDOR TEST")

        # 3. Verificar que el archivo en cuarentena existe
        q_path = data_manager.get_quarantine_path(entry_id)
        self.assertIsNotNone(q_path)
        self.assertTrue(os.path.exists(q_path))

    def test_vendor_rules_learning(self):
        """Verifica que las reglas por proveedor se memoricen y consulten con normalización."""
        # Guardar regla para "ACME SUPPLIES S.L."
        rule = data_manager.save_vendor_rule(
            supplier_name="ACME SUPPLIES S.L.",
            date_format="DD/MM/YYYY",
            always_accept=True,
            notes="Prueba unitaria",
        )
        self.assertEqual(rule["date_format"], "DD/MM/YYYY")
        self.assertTrue(rule["always_accept"])

        # Consultar con variaciones de nombre (minúsculas, sin SL, etc.)
        matched = data_manager.get_vendor_rule("Acme Supplies")
        self.assertIsNotNone(matched)
        self.assertEqual(matched["date_format"], "DD/MM/YYYY")

        matched_exact = data_manager.get_vendor_rule("ACME SUPPLIES S.L.")
        self.assertIsNotNone(matched_exact)
        self.assertTrue(matched_exact["always_accept"])

    def test_ambiguity_detection_structure(self):
        """Verifica que se registre una factura dudosa con sus opciones de fecha."""
        entry_id = data_manager.add_history_entry(
            status="AMBIGUOUS_DATE",
            filename="factura_dudosa.pdf",
            supplier="GLOBAL TOOLS",
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
        self.assertEqual(len(entry["detected_dates"]), 2)

    def test_fastapi_endpoints(self):
        """Prueba los endpoints REST de la API local."""
        # 1. Status
        res = self.client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("status", data)
        self.assertIn("stats", data)

        # 2. History
        res = self.client.get("/api/history")
        self.assertEqual(res.status_code, 200)

        # 3. Discarded
        res = self.client.get("/api/discarded")
        self.assertEqual(res.status_code, 200)

        # 4. Ambiguous
        res = self.client.get("/api/ambiguous")
        self.assertEqual(res.status_code, 200)

        # 5. Schedule GET & POST
        res = self.client.get("/api/schedule")
        self.assertEqual(res.status_code, 200)

        res = self.client.post("/api/schedule", json={
            "enabled": True,
            "frequency": "weekly",
            "day_of_week": 0,
            "day_of_month": 1,
            "hour": 8,
            "minute": 30,
        })
        self.assertEqual(res.status_code, 200)
        saved = res.json()["schedule"]
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["hour"], 8)
        self.assertEqual(saved["minute"], 30)

        # 6. Rules GET
        res = self.client.get("/api/rules")
        self.assertEqual(res.status_code, 200)
        self.assertIn("rules", res.json())


if __name__ == "__main__":
    unittest.main()
