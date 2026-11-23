"""
test_carpeta_proveedor_y_mover.py — Carpeta de proveedor en Drive y resolución de fechas dudosas.

A. invoice_manager.get_or_create_supplier_folder -> qué carpeta de proveedor se reutiliza y
   cuándo se crea una nueva. Una palabra genérica compartida ('SUPERMERCADOS', 'SOL'), una
   subcadena dentro de otra palabra ('MVA' dentro de 'MVAX') o un nombre distinto no bastan.
B. dashboard/app.py, POST /api/resolve_ambiguity -> si Drive no puede mover o subir el archivo,
   la factura sigue en Dudosas y no se guarda ninguna regla de formato.

Sin red: el servicio de Drive es un objeto falso que solo anota las llamadas; la autenticación,
las carpetas y las subidas o movimientos se sustituyen. Los datos del usuario van a una carpeta
temporal y no se muestra salida por consola.

Ejecutar desde la carpeta del proyecto:
    python -W ignore -m unittest test_carpeta_proveedor_y_mover -v
"""

import os
import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import data_manager
import invoice_manager
from dashboard.app import app
from test_email_attachments import IsolatedDataTestCase, PDF_BYTES


# ── Datos de prueba (falsos) ─────────────────────────────────────────────────

# Carpetas de proveedor que ya existen en el mes. El 'id' es un nombre corto para leer resultados.
CARPETAS_EXISTENTES = [
    {"id": "lidl", "name": "LIDL SUPERMERCADOS S.A.U."},
    {"id": "env", "name": "ENVÍOMÉDICAL, S.L."},
    {"id": "orona", "name": "ORONA-GALO"},
    {"id": "maeso", "name": "MAESO GESTÍ"},
    {"id": "carr", "name": "CARREFOUR"},
    {"id": "sol", "name": "PAPELERÍA SOL"},
    {"id": "mva", "name": "MVA"},
]

# Cuerpo de la petición de /api/resolve_ambiguity: fecha confirmada por el usuario
PETICION_FECHA = {"day": 5, "month": 10, "year": 2026, "format": "DD/MM/YYYY"}


# ── Servicio de Drive falso (sin red) ────────────────────────────────────────

class _Peticion:
    """Imita una petición de la API de Drive: solo tiene execute()."""

    def __init__(self, resultado):
        self._resultado = resultado

    def execute(self):
        return self._resultado


class _Ficheros:
    """Imita service.files(): list() devuelve las carpetas del servicio y create() las anota."""

    def __init__(self, servicio):
        self._servicio = servicio

    def list(self, **kwargs):
        return _Peticion({"files": [dict(c) for c in self._servicio.carpetas]})

    def create(self, **kwargs):
        self._servicio.creadas.append(kwargs)
        return _Peticion({"id": f"nueva-{len(self._servicio.creadas)}"})


class ServicioDriveFalso:
    """Servicio de Drive sin red: 'carpetas' son las existentes y 'creadas' las que se van creando."""

    def __init__(self, carpetas):
        self.carpetas = carpetas
        self.creadas = []

    def files(self):
        return _Ficheros(self)


# ── A. get_or_create_supplier_folder ─────────────────────────────────────────

class TestA_CarpetaDeProveedor(IsolatedDataTestCase):
    """A. Reutiliza la carpeta de proveedor correcta o crea una nueva."""

    def setUp(self):
        super().setUp()
        # Sin alias locales: el supplier_aliases.json de cada instalación no debe cambiar el resultado
        self.patch_attr(invoice_manager, "SUPPLIER_ALIASES", new={})
        self.patch_attr(invoice_manager, "TAX_ID_SUPPLIER_MAP", new={})

    def buscar(self, nombre):
        """Ejecuta la función real. Devuelve (id devuelto, lista de carpetas creadas en Drive)."""
        servicio = ServicioDriveFalso([dict(c) for c in CARPETAS_EXISTENTES])
        carpeta = invoice_manager.get_or_create_supplier_folder(servicio, nombre, "mes-padre")
        return carpeta, servicio.creadas

    def assertCreaCarpeta(self, nombre):
        """Debe crearse exactamente una carpeta nueva, con ese nombre en mayúsculas, y no reutilizar ninguna."""
        carpeta, creadas = self.buscar(nombre)
        self.assertEqual(len(creadas), 1, f"Se esperaba crear 1 carpeta y se crearon {len(creadas)}")
        self.assertEqual(creadas[0]["body"]["name"], nombre.upper())
        self.assertEqual(carpeta, "nueva-1", f"'{nombre}' no debía reutilizar una carpeta existente")

    def assertReutilizaCarpeta(self, nombre, carpeta_esperada):
        """Debe devolver la carpeta existente indicada, sin crear ninguna."""
        carpeta, creadas = self.buscar(nombre)
        self.assertEqual(carpeta, carpeta_esperada)
        self.assertEqual(creadas, [], f"'{nombre}' no debía crear carpeta nueva")

    def test_A1_palabra_generica_compartida_no_une_proveedores(self):
        """ALDI no es LIDL aunque ambos tengan 'SUPERMERCADOS' (E7): crea su propia carpeta."""
        self.assertCreaCarpeta("ALDI MASQUEFA SUPERMERCADOS, S.L.U.")

    def test_A2_palabra_comun_no_es_la_marca(self):
        """'SOL' dentro de 'DISTRIBUCIONES SOL LEVANTE' no es la marca 'PAPELERÍA SOL'."""
        self.assertCreaCarpeta("DISTRIBUCIONES SOL LEVANTE SL")

    def test_A3_subcadena_dentro_de_otra_palabra_no_coincide(self):
        """'MVAX TRADING' no es 'MVA': una subcadena dentro de otra palabra no cuenta."""
        self.assertCreaCarpeta("MVAX TRADING")

    def test_A4_nombre_igual_tras_normalizar_reutiliza_la_carpeta(self):
        """'ENVÍOMÉDICAL' coincide con 'ENVÍOMÉDICAL, S.L.' tras quitar sufijos y puntuación."""
        self.assertReutilizaCarpeta("ENVÍOMÉDICAL", "env")

    def test_A5_nombre_exacto_reutiliza_la_carpeta(self):
        """'CARREFOUR' coincide exactamente con la carpeta 'CARREFOUR'."""
        self.assertReutilizaCarpeta("CARREFOUR", "carr")

    def test_A6_misma_marca_con_otro_sufijo_reutiliza_la_carpeta(self):
        """'ORONA S. COOP.' es la misma marca que 'ORONA-GALO' (primera palabra igual)."""
        self.assertReutilizaCarpeta("ORONA S. COOP.", "orona")

    def test_A7_acento_y_sufijo_no_impiden_reutilizar_la_carpeta(self):
        """'MAESO GESTÍ S.L.' coincide con la carpeta 'MAESO GESTÍ'."""
        self.assertReutilizaCarpeta("MAESO GESTÍ S.L.", "maeso")


# ── B. resolve_ambiguity (POST /api/resolve_ambiguity/{id}) ──────────────────

class TestB_ResolverFechaDudosa(IsolatedDataTestCase):
    """B. Si Drive falla, la factura sigue en Dudosas y no se aprende la regla de formato."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(app)
        self.fuente = os.path.join(self.test_data_dir, "Scan 37.pdf")
        with open(self.fuente, "wb") as f:
            f.write(PDF_BYTES)
        # Drive y la red se sustituyen; el historial y las reglas son reales (en carpeta temporal)
        self.patch_attr(invoice_manager, "DRIVE_ROOT_ID", new="raiz-falsa")
        self.patch_attr(invoice_manager, "authenticate_drive", return_value=MagicMock(name="drive"))
        self.patch_attr(invoice_manager, "get_or_create_folder",
                        side_effect=lambda servicio, nombre, padre: f"id-{nombre}")
        self.patch_attr(invoice_manager, "get_or_create_supplier_folder", return_value="new")
        self.mover = self.patch_attr(invoice_manager, "move_drive_file", return_value=True)
        self.subir = self.patch_attr(invoice_manager, "upload_to_drive", return_value=None)

    def dudosa(self, con_drive=True):
        """Factura dudosa de 'WUMY MARKER S.L.'. Con con_drive ya está en Drive (archivo 'f1',
        carpeta 'old'); si no, solo tiene su copia en cuarentena."""
        return data_manager.add_history_entry(
            status="AMBIGUOUS_DATE", filename="Scan 37.pdf", supplier="WUMY MARKER S.L.",
            date_str="09-26",
            drive_file_id="f1" if con_drive else None,
            drive_folder_id="old" if con_drive else None,
            original_filepath=self.fuente)

    def resolver(self, item):
        return self.client.post(f"/api/resolve_ambiguity/{item}", json=PETICION_FECHA)

    def reglas(self):
        return data_manager.load_vendor_rules()["rules"]

    def test_B1_drive_no_mueve_el_archivo_la_factura_sigue_en_dudosas(self):
        """Si move_drive_file devuelve False, la petición falla, la entrada no cambia y no se guarda regla."""
        self.mover.return_value = False
        item = self.dudosa()

        res = self.resolver(item)

        self.mover.assert_called_once()
        self.assertGreaterEqual(res.status_code, 400, f"Debía fallar y respondió {res.status_code}: {res.text}")
        entrada = data_manager.get_history_entry(item)
        self.assertEqual(entrada["status"], "AMBIGUOUS_DATE")
        self.assertEqual(entrada["drive_folder_id"], "old")
        self.assertEqual(self.reglas(), {}, "No debe guardarse ninguna regla si Drive falló")

    def test_B2_drive_mueve_el_archivo_la_factura_queda_archivada(self):
        """Si move_drive_file devuelve True, la factura pasa a SUCCESS en la carpeta nueva y se guarda la regla."""
        item = self.dudosa()

        res = self.resolver(item)

        self.assertEqual(res.status_code, 200, res.text)
        self.mover.assert_called_once()
        entrada = data_manager.get_history_entry(item)
        self.assertEqual(entrada["status"], "SUCCESS")
        self.assertEqual(entrada["drive_folder_id"], "new")
        self.assertEqual(len(self.reglas()), 1, "Tras el éxito debe guardarse la regla de formato")

    def test_B3_sin_copia_en_drive_y_subida_fallida_sigue_en_dudosas(self):
        """Sin archivo en Drive, si upload_to_drive devuelve None, la petición falla y la entrada sigue igual."""
        self.subir.return_value = None
        item = self.dudosa(con_drive=False)
        self.assertIsNotNone(data_manager.get_quarantine_path(item), "Precondición: debe existir la copia en cuarentena")

        res = self.resolver(item)

        self.subir.assert_called_once()
        self.assertGreaterEqual(res.status_code, 400, f"Debía fallar y respondió {res.status_code}: {res.text}")
        entrada = data_manager.get_history_entry(item)
        self.assertEqual(entrada["status"], "AMBIGUOUS_DATE")
        self.assertIsNone(entrada["drive_file_id"])
        self.assertEqual(self.reglas(), {}, "No debe guardarse ninguna regla si la subida falló")

    def test_B4_sin_copia_en_drive_y_subida_correcta_guarda_el_id_nuevo(self):
        """Sin archivo en Drive, si la subida devuelve 'f9', la factura queda archivada con ese id."""
        self.subir.return_value = "f9"
        item = self.dudosa(con_drive=False)
        self.assertIsNotNone(data_manager.get_quarantine_path(item), "Precondición: debe existir la copia en cuarentena")

        res = self.resolver(item)

        self.subir.assert_called_once()
        self.assertEqual(res.status_code, 200, res.text)
        entrada = data_manager.get_history_entry(item)
        self.assertEqual(entrada["status"], "SUCCESS")
        self.assertEqual(entrada["drive_file_id"], "f9")
        self.assertEqual(entrada["drive_folder_id"], "new")


if __name__ == "__main__":
    unittest.main()
