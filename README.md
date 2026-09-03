# 📄 Invoice Manager — Gestor Autónomo de Facturas

> 🚑 **¿Has cambiado de ordenador o necesitas reinstalar/restaurar todo desde cero?**  
> Consulta la **[Guía de Restauración Paso a Paso (Para No Informáticos)](GUIA_RESTAURACION.md)** con instrucciones detalladas, sin tecnicismos y listas de comprobación.  
> 📜 Consulta el **[Historial de Cambios (Changelog)](CHANGELOG.md)** para ver todas las novedades y evolución del proyecto.

Script de Python que automatiza la gestión de facturas con **Gemini 3.5 Flash-Lite**:

1. **Busca** en Gmail correos no leídos con facturas adjuntas
2. **Ignora** los correos de proveedores gestionados por otro canal (p. ej. EnvíoMédical)
3. **Valida** cada adjunto con Gemini 3.5 Flash-Lite (anti-falsos positivos)
4. **Filtra** documentos que no son facturas reales (albaranes, presupuestos, confirmaciones...)
5. **Verifica CIF/NIF/VAT** del emisor como requisito obligatorio
6. **Detecta proveedores Amazon** automáticamente por dominio del remitente
7. **Descarga las facturas del portal B2B de EnvíoMédical** (env.titaniatools.es) automáticamente
8. **Sube** el archivo a Google Drive en carpetas `Año / MM-YY / PROVEEDOR`
9. **Marca** el correo como leído para no reprocesarlo

---

## 🏥 Flujo especial: EnvíoMédical (portal B2B)

Las facturas que EnvíoMédical envía por correo **no se procesan** (se marcan como leídas y se descartan). En su lugar, en cada ejecución el script hace una **Fase 2**:

> 🛡️ **Doble red de seguridad**: además del filtro por remitente, el validador de Gemini rechaza explícitamente los documentos cuyo número empieza por "FW" (Factura Web de EnvíoMédical), por si alguno llegara disfrazado desde otra dirección de correo.

1. Inicia sesión en el portal B2B (`env.titaniatools.es`) con las credenciales del `.env`
2. Consulta la lista de facturas **desde la última ejecución** (con un margen de seguridad de 45 días por defecto)
3. Compara con el registro local `enviomedical_state.json` y **solo descarga las nuevas**
4. Sube cada PDF a Drive en `Año / MM-YY / ENVÍOMÉDICAL` (reutiliza la carpeta `ENVÍOMÉDICAL, S.L.` si ya existe)
5. Registra cada factura en el estado local; las que fallan se reintentan la próxima vez

La fecha de carpeta sale de la propia tabla del portal (sin pasar por Gemini), y hay doble protección contra duplicados: estado local + comparación MD5 en Drive.

**Variables del `.env`:**

| Variable | Obligatoria | Descripción |
|---|---|---|
| `ENVIO_USER` | Sí (para Fase 2) | Usuario del portal B2B |
| `ENVIO_PASS` | Sí (para Fase 2) | Contraseña del portal B2B |
| `ENVIO_SUPPLIER_NAME` | No | Nombre de carpeta en Drive (defecto: `ENVÍOMÉDICAL`) |
| `ENVIO_LOOKBACK_DAYS` | No | Margen de búsqueda en días (defecto: `45`) |
| `IGNORE_SENDER_DOMAINS` | No | Dominios de correo a ignorar (defecto: `enviomedical.com`) |

> El archivo `enviomedical_state.json` se genera solo. Si se borra, la próxima ejecución volverá a consultar los últimos 45 días; Drive descartará los duplicados por MD5.

---

## ✨ Características Principales

### Jerarquía de Validación
El sistema aplica una jerarquía de decisiones estricta:

```
Email Sender > Validación Fiscal > Contenido PDF
```

- **Paso A — Override por remitente:** Si el email viene de `@amazon.*`, el proveedor se fuerza a "AMAZON"
- **Paso B — Filtro fiscal:** Si no se detecta un CIF/NIF/VAT válido, el documento se descarta
- **Paso C — Fecha:** Se parsea la fecha con soporte para formatos europeos

### Validación Inteligente de Facturas
- Distingue facturas reales de otros documentos (albaranes, presupuestos, confirmaciones de pedido)
- Usa **pdfplumber** como fallback para extraer CIF/NIF directamente del texto del PDF
- Regex especializado para códigos fiscales europeos e intracomunitarios

### Gestión Inteligente de Proveedores en Drive
- Match **exacto**, **parcial** y por **palabra clave** normalizado
- Elimina sufijos legales (S.L., S.A., LTD, GmbH...) para comparaciones
- Quita acentos y diacríticos para evitar duplicados

### Rate Limiting y Reintentos
- Espera configurable entre llamadas a Gemini (4s por defecto)
- Reintentos automáticos con backoff exponencial en caso de rate limit (429)
- Los correos con errores de API NO se marcan como leídos para reprocesarlos

---

## 🔧 Configuración Paso a Paso

### Paso 1: Generar una Contraseña de Aplicación de Google

La contraseña de aplicación permite que este script acceda a tu Gmail sin exponer tu contraseña real. **Necesitas tener la Verificación en 2 Pasos activada.**

1. Ve a [myaccount.google.com](https://myaccount.google.com) e inicia sesión
2. Ve a **Seguridad** → **Cómo inicias sesión en Google**
3. Verifica que la **Verificación en 2 pasos** está **activada**
4. Ve a [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
5. Nombre de la app: `Invoice Manager` → **Crear**
6. Copia la contraseña de **16 caracteres** que se genera

> ⚠️ **¡IMPORTANTE!** Esta contraseña solo se muestra UNA VEZ.

---

### Paso 2: Obtener una API Key de Gemini

1. Ve a [Google AI Studio](https://aistudio.google.com/apikey)
2. Haz clic en **Create API Key** y copia la clave

---

### Paso 3: Configurar Google Drive API

1. Ve a [Google Cloud Console](https://console.cloud.google.com)
2. Crea un proyecto nuevo (o usa uno existente)
3. **Habilita la API de Google Drive:**
   - Ve a **APIs y Servicios** → **Biblioteca** → Busca **Google Drive API** → **Habilitar**
4. **Crea las credenciales OAuth:**
   - Ve a **APIs y Servicios** → **Credenciales** → **+ CREAR CREDENCIALES** → **ID de cliente de OAuth**
   - Tipo de aplicación: **App de escritorio**
   - Nombre: `Invoice Manager Desktop`
5. **Descarga el archivo JSON:**
   - Renómbralo a **`credentials.json`**
   - Muévelo a la carpeta de este proyecto

---

### Paso 4: Instalar Dependencias

```bash
pip install -r requirements.txt
```

---

### Paso 5: Crear el archivo `.env`

```bash
cp .env.example .env
```

Edita `.env` con tus valores:

```env
EMAIL_ADDRESS=tu-email@ejemplo.com
APP_PASSWORD=xxxx xxxx xxxx xxxx
GEMINI_API_KEY=tu-api-key-de-gemini
DRIVE_ROOT_FOLDER_ID=tu-id-de-carpeta-raiz
```

---

## 🚀 Uso

```bash
python invoice_manager.py
```

La **primera vez** se abrirá tu navegador para autorizar el acceso a Google Drive. Después se guardará un `token.json` automáticamente.

### ¿Qué hace exactamente?

```
1. Se conecta a Gmail via IMAP
2. Busca correos no leídos con "factura", "facturas" o "invoice" + adjunto
3. Para cada correo encontrado:
   a. Descarga los adjuntos (PDF, PNG, JPG, TIFF, WebP)
   b. Los envía a Gemini 3.5 Flash-Lite para validar:
      - ¿Es una factura real? (descarta albaranes, presupuestos, etc.)
      - Nombre del proveedor
      - CIF/NIF/VAT del emisor
      - Fecha de la factura
   c. Valida que tenga un CIF/NIF/VAT válido
   d. Si el remitente es @amazon.*, fuerza proveedor = AMAZON
   e. Crea en Google Drive la estructura:
      📁 [Carpeta Raíz]
       └── 📁 2026 (Año)
            └── 📁 02-26 (MM-YY)
                 └── 📁 PROVEEDOR
                      └── 📄 factura.pdf
   f. Marca el correo como leído
4. Fin → No se reprocesarán la próxima vez
```

---

## 📁 Estructura del Proyecto

```text
invoice-manager/
├── invoice_manager.py           ← Script principal (Gemini + IMAP + Drive)
├── enviomedical_portal.py       ← Módulo Fase 2 (Portal B2B EnvíoMédical)
├── GUIA_RESTAURACION.md         ← Guía completa paso a paso para no informáticos
├── CHANGELOG.md                 ← Historial detallado de versiones y mejoras
├── requirements.txt             ← Dependencias de Python
├── .env.example                 ← Plantilla de configuración
├── .env                         ← Tu configuración con contraseñas (NO se sube a git)
├── credentials.json             ← Credenciales OAuth de Google (NO se sube a git)
├── token.json                   ← Token generado automáticamente (NO se sube a git)
├── supplier_aliases.example.json← Plantilla para mapeo opcional de alias/CIFs
└── supplier_aliases.json        ← Tus alias de proveedores locales (NO se sube a git)
```

---

## 🛡️ Seguridad

Los archivos sensibles están estrictamente excluidos del repositorio vía `.gitignore`:
- `.env` — Contraseñas y claves API
- `credentials.json` — Credenciales OAuth de Google
- `token.json` — Token de sesión de Google Drive
- `enviomedical_state.json` — Registro local de sincronización
- `supplier_aliases.json` — Mapeos privados de nombres fiscales y CIFs
- `Facturas ejemplo/` — Facturas y documentos en PDF

---

## ❓ Solución de Problemas

| Problema | Solución |
|---|---|
| `AUTHENTICATE failed` | Verifica que la App Password es correcta y la Verificación en 2 pasos está activa |
| `credentials.json not found` | Sigue el Paso 3 para crear y descargar las credenciales OAuth |
| `Rate limit (429)` | El script reintenta automáticamente con backoff. Si persiste, espera unos minutos |
| Sin CIF/NIF → Descartado | Es el comportamiento esperado: solo se procesan facturas con identificación fiscal |
| Gemini devuelve `DESCONOCIDO` | El PDF puede estar escaneado; la calidad de imagen importa |
| No encuentra correos | Verifica que hay correos no leídos con las palabras clave y adjuntos |
