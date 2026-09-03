import os
from pathlib import Path
from invoice_manager import init_gemini, validate_is_invoice, sanitize_folder_name, INVOICE_EXTENSIONS

def main():
    test_dir = "test_invoices"
    
    # Crear la carpeta local si no existe
    if not os.path.exists(test_dir):
        os.makedirs(test_dir)
        print(f"Se ha creado la carpeta '{test_dir}'.")
        print("Por favor, coloca algunos archivos PDF o imagenes de prueba dentro de ella y vuelve a ejecutar este script.")
        return

    # Buscar archivos con las extensiones válidas de facturas
    files_to_test = [
        os.path.join(test_dir, f) 
        for f in os.listdir(test_dir) 
        if Path(f).suffix.lower() in INVOICE_EXTENSIONS
    ]

    if not files_to_test:
        print(f"No hay facturas en la carpeta '{test_dir}'. Coloca algunas para empezar.")
        return

    print(f"Iniciando simulador con {len(files_to_test)} archivo(s)...")
    
    # Inicializar Gemini directamente desde la lógica central
    model = init_gemini()
    
    # Simular una estructura de Drive en memoria para detectar duplicados { "Ruta/De/Carpeta": set(nombres_de_archivo) }
    simulated_drive = {}
    
    total = len(files_to_test)
    errors = 0
    success = 0
    
    for filepath in files_to_test:
        filename = os.path.basename(filepath)
        print(f"\n{'='*60}")
        print(f"Simulando archivo local: {filename}")
        
        # Simular el correo del remitente para la regla de Amazon
        # Si el nombre del archivo contiene 'amazon', simulamos que el correo viene de allí.
        sender_mock = ""
        if "amazon" in filename.lower():
            sender_mock = "facturas-noreply@amazon.es"
            
        try:
            # LÓGICA IDÉNTICA: Reutilizamos validate_is_invoice tal cual
            info = validate_is_invoice(model, filepath, sender_email=sender_mock)
            
            if info is None:
                print("  [SIMULADOR] El archivo fue descartado (Documento no es factura o sin CIF valido).")
                errors += 1
                continue
                
            # Regla de Mayúsculas estricta
            supplier_upper = sanitize_folder_name(info["supplier"]).upper()
            date_str = info["date"] # FORMATO EUROPEO MM-YY
            
            # Parsear el año
            parts = date_str.split("-")
            month_part, year_short = parts[0], parts[1]
            year_folder = f"20{year_short}"
            
            folder_path = f"{year_folder}/{date_str}/{supplier_upper}"
            
            # Simulación de Drive visual
            print(f"CARPETA: Crearía {folder_path}")
            
            if folder_path not in simulated_drive:
                simulated_drive[folder_path] = set()
                
            if filename in simulated_drive[folder_path]:
                print(f"DUPLICADO: El archivo ya existe, se saltaría la subida.")
            else:
                simulated_drive[folder_path].add(filename)
                print(f"ARCHIVO: Subiría {filename}")
                
            success += 1
            
        except Exception as e:
            print(f"  [ERROR] Excepcion inesperada evaluando {filename}: {e}")
            errors += 1

    # Informe Final
    print(f"\n{'='*60}")
    print(f"Total facturas simuladas: {total}. Errores detectados: {errors}.")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
