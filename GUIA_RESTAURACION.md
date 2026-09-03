# 🚑 Guía de Restauración Paso a Paso (Para No Informáticos)

> **¿Se te ha roto el ordenador, formateado el sistema o quieres instalar el gestor de facturas en un PC nuevo desde cero?**  
> Sigue esta guía punto por punto. No necesitas saber de programación. Cada paso está explicado de forma directa y sencilla.

---

## 📋 Resumen Rápido: ¿Qué archivos necesita el programa para funcionar?

En GitHub **solo está el código fuente limpio**. Por motivos de seguridad y privacidad, **las claves y contraseñas NUNCA están en GitHub**.

Para que el programa funcione en tu ordenador nuevo, la carpeta del proyecto debe terminar teniendo estos **3 archivos clave** que tú aportarás:

1. **`credentials.json`** ➔ Lo descargas de Google Cloud (autoriza el acceso a tu Google Drive).
2. **`.env`** ➔ Lo creas tú con el Bloc de Notas (contiene tu correo, tu contraseña de Gmail y tu clave de Gemini).
3. **`token.json`** ➔ **Se genera solo** la primera vez que ejecutes el programa tras darle a "Permitir" en tu navegador.

---

## 🛠️ PASO 1: Instalar los programas necesarios en el nuevo PC

Antes de descargar el proyecto, tu ordenador necesita tener **Python** y **Git**:

1. **Instalar Python:**
   - Entra en [python.org/downloads](https://www.python.org/downloads/) y descarga la versión recomendada para Windows.
   - Abre el instalador descargado.
   - ⚠️ **MUY IMPORTANTE (EL ERROR MÁS COMÚN):** En la primera pantalla del instalador, marca la casilla que dice:
     > **☑ Add python.exe to PATH** (abajo del todo).
   - Haz clic en **Install Now** y espera a que termine.

2. **Instalar Git:**
   - Entra en [git-scm.com/download/win](https://git-scm.com/download/win) y descárgalo.
   - Instálalo dejando todas las opciones por defecto (siguiente, siguiente, instalar).

---

## 📥 PASO 2: Descargar el proyecto a tu ordenador

1. Abre la aplicación **PowerShell** o **Símbolo del sistema (cmd)** en Windows.
2. Escribe dónde quieres guardar la carpeta (por ejemplo, en tus Documentos o en una carpeta de proyectos).
3. Escribe este comando y pulsa **Enter**:
   ```bash
   git clone https://github.com/StormJelly87/Facturas-Manager-3.1-Flash-Lite-free-version-V1.2.git
   ```
   *(También puedes entrar al enlace de GitHub en tu navegador, pulsar el botón verde **Code** ➔ **Download ZIP**, y descomprimir la carpeta donde tú quieras).*
4. Entra en la carpeta descargada:
   ```bash
   cd Facturas-Manager-3.1-Flash-Lite-free-version-V1.2
   ```

---

## 📦 PASO 3: Instalar las librerías del proyecto

Las librerías son las herramientas que Python necesita para leer PDFs, conectarse a Google y hablar con la inteligencia artificial.

Dentro de la carpeta en PowerShell, escribe:
```bash
pip install -r requirements.txt
```
Pulsa **Enter** y espera un minuto a que se descarguen e instalen solas.

---

## 🔑 PASO 4: Crear tu archivo `.env` (Tus contraseñas secretas)

### ¿Se tiene que generar un archivo `.env`?
**SÍ, rotundamente SÍ.** En GitHub verás un archivo llamado `.env.example`. Ese archivo es solo una plantilla de muestra. Tienes que crear tu propio archivo `.env` real.

### ¿Cómo crearlo paso a paso?
1. En la carpeta del proyecto, verás el archivo `.env.example`.
2. Haz una copia de ese archivo y renómbrala exactamente a:
   ```text
   .env
   ```
   *(Nota: Empieza por un punto `.`. Si Windows te dice algo, ábrelo con el **Bloc de Notas** y dale a "Guardar como" con nombre `".env"` y tipo "Todos los archivos")*.

3. Abre ese archivo `.env` con el **Bloc de Notas**. Verás esto:
   ```env
   # ── Gmail (IMAP) ──────────────────────────────────────────────
   EMAIL_ADDRESS=tu-email@ejemplo.com
   APP_PASSWORD=xxxx xxxx xxxx xxxx

   # ── Gemini API ────────────────────────────────────────────────
   GEMINI_API_KEY=tu-api-key-de-gemini

   # ── Google Drive ──────────────────────────────────────────────
   DRIVE_ROOT_FOLDER_ID=tu-id-de-carpeta-raiz

   # ── Portal B2B EnvioMedical (Titania Tools) ───────────────────
   ENVIO_USER=tu-email@ejemplo.com
   ENVIO_PASS=tu-contrasena-del-portal
   ```

4. **Ahora rellena cada línea con tus datos reales:**

   * **`EMAIL_ADDRESS`:**  
     Tu dirección completa de Gmail donde recibes las facturas (ej: `miempresa@gmail.com`).

   * **`APP_PASSWORD` (Contraseña de aplicación de Google de 16 letras):**  
     ⚠️ *No es tu contraseña normal de Gmail.* Es una clave especial para scripts:
     1. Entra a [myaccount.google.com/security](https://myaccount.google.com/security).
     2. Asegúrate de tener la **Verificación en dos pasos** activada.
     3. Entra directamente a [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
     4. En nombre escribe `Gestor Facturas` y pulsa **Crear**.
     5. Te saldrá un cartel amarillo con una clave de **16 letras con espacios** (ejemplo: `abcd efgh ijkl mnop`).
     6. Cópiala tal cual y pégala en `APP_PASSWORD`.

   * **`GEMINI_API_KEY` (Clave de Inteligencia Artificial):**  
     1. Entra en [aistudio.google.com/apikey](https://aistudio.google.com/apikey) con tu cuenta de Google.
     2. Pulsa en **Create API key** (Crear clave de API).
     3. Copia el texto largo que te da y pégalo en `GEMINI_API_KEY`.

   * **`DRIVE_ROOT_FOLDER_ID` (Carpeta de Google Drive):**  
     1. Abre [Google Drive](https://drive.google.com) en tu navegador.
     2. Entra en la carpeta donde quieres que se guarden las facturas.
     3. Mira la barra de direcciones de tu navegador web arriba. Verás una dirección como esta:  
        `https://drive.google.com/drive/folders/1a2B3c4D5e6F7g8H9i0jKlMnOpQrStUvW`
     4. La última parte después de `folders/` es tu ID:  
        En este ejemplo sería `1a2B3c4D5e6F7g8H9i0jKlMnOpQrStUvW`.  
        Cópialo y pégalo en `DRIVE_ROOT_FOLDER_ID`.

   * **`ENVIO_USER` y `ENVIO_PASS` (Opcional - solo si usas el portal EnvíoMédical):**  
     Tu usuario y contraseña con los que entras a la página de Titania Tools / EnvíoMédical. Si no usas este portal, puedes dejarlo vacío o borrar esas dos líneas.

5. Guarda los cambios en el Bloc de Notas (`Ctrl + S`) y ciérralo.

---

## 📄 PASO 5: Colocar el archivo `credentials.json` (Google Drive)

### ¿Qué es este archivo?
Es el archivo de permisos oficial que Google Cloud te entrega para autorizar a tu script a conectarse a tu Google Drive sin riesgos.

### ¿Dónde tiene que estar colocado?
Tiene que estar **dentro de la carpeta principal del proyecto**, exactamente con este nombre:
```text
credentials.json
```
*(Al lado de `invoice_manager.py` y del `.env`)*.

### ¿Cómo conseguirlo si no tienes una copia guardada?
Si perdiste el archivo cuando se rompió tu PC anterior:
1. Entra a [Google Cloud Console](https://console.cloud.google.com/).
2. Inicia sesión con la misma cuenta de Google donde tienes tu Google Drive.
3. Arriba a la izquierda, pulsa en el desplegable de proyectos y pulsa **Nuevo Proyecto** (nómbralo por ejemplo `Gestor Facturas`).
4. En el menú de la izquierda ve a: **APIs y servicios** ➔ **Biblioteca**.
5. Busca `Google Drive API` y pulsa **Habilitar**.
6. En el menú de la izquierda ve a: **APIs y servicios** ➔ **Pantalla de consentimiento de OAuth**:
   - Elige **Externo** y pulsa Crear.
   - Ponle nombre a la app (`Facturas Manager`), tu email de contacto y guarda.
   - En "Usuarios de prueba", añade tu propio correo electrónico de Gmail.
7. Ve a: **APIs y servicios** ➔ **Credenciales**:
   - Pulsa arriba en **+ CREAR CREDENCIALES** ➔ **ID de cliente de OAuth**.
   - En *Tipo de aplicación*, selecciona **App de escritorio** (Desktop app).
   - En nombre pon `Desktop Client` y pulsa **Crear**.
8. Te saldrá una ventana con un botón que dice **Descargar JSON**.
9. Descarga ese archivo, cámbiale el nombre a **`credentials.json`** y muévelo dentro de la carpeta del proyecto.

---

## 🏷️ PASO 6 (Opcional): Mapeo de Proveedores (`supplier_aliases.json`)

Si tienes proveedores cuyas facturas vienen a nombre fiscal de una persona o con un CIF/NIE concreto y quieres que en Google Drive se guarden con su nombre comercial habitual (por ejemplo, si la razón social `JUAN PEREZ GARCIA` o el CIF `Y1234567Z` quieres que se guarde como `SUMINISTROS PEREZ`):

1. En la carpeta verás el archivo plantilla `supplier_aliases.example.json`.
2. Crea una copia y llámala `supplier_aliases.json`.
3. Edítala con el Bloc de Notas añadiendo tus nombres según necesites:
   ```json
   {
     "aliases": {
       "JUAN PEREZ GARCIA": "SUMINISTROS PEREZ",
       "JUAN PEREZ": "SUMINISTROS PEREZ"
     },
     "tax_id_map": {
       "Y1234567Z": "SUMINISTROS PEREZ"
     }
   }
   ```
*(Este archivo está protegido por `.gitignore` y nunca se subirá a internet)*.

---

## 🚀 PASO 7: Primera Ejecución (Generación de `token.json`)

Ahora que ya tienes:
- [x] Python y librerías instaladas
- [x] Archivo `.env` configurado
- [x] Archivo `credentials.json` colocado en la carpeta

Abre PowerShell en la carpeta del proyecto y ejecuta:
```bash
python invoice_manager.py
```

### ¿Qué ocurrirá la primera vez?
1. En tu pantalla se abrirá automáticamente tu navegador de internet de Google.
2. Te pedirá que elijas tu cuenta de Google.
3. Te saldrá una pantalla de advertencia: *"Google no ha verificado esta aplicación"*.  
   **No te asustes:** es totalmente normal porque la aplicación la has creado tú mismo en tu propia cuenta.
4. Haz clic en **Configuración avanzada** (abajo a la izquierda).
5. Haz clic en **Ir a Gestor Facturas (no seguro)**.
6. Marca las casillas de permisos para Google Drive y dale a **Continuar**.
7. Verás un mensaje en el navegador: *"The authentication flow has completed. You may close this window"*.
8. ¡Listo! Vuelve a tu terminal. Verás que en tu carpeta ha aparecido automáticamente un archivo llamado **`token.json`**.
   > **A partir de este momento, nunca más te volverá a pedir que abras el navegador.** El programa funcionará 100% solo.

---

## 🔄 PASO 8: Automatización diaria (Opcional)

Si quieres que el programa se ejecute solo todos los días sin que tú tengas que abrir nada:

1. En Windows, pulsa la tecla **Inicio** y busca **Programador de tareas**.
2. A la derecha, pulsa en **Crear tarea básica...**.
3. Nombre: `Gestor de Facturas Diario`.
4. Desencadenador: **Diariamente** (por ejemplo, a las 20:00).
5. Acción: **Iniciar un programa**.
6. Programa o script: `python` (o la ruta completa `python.exe`).
7. Argumentos: `invoice_manager.py`
8. Iniciar en (carpeta): Pega la ruta completa de tu carpeta (ej: `C:\Facturas-Manager`).
9. Pulsa Finalizar. Cada día a esa hora revisará tu Gmail y archivará tus facturas automáticamente.

---

## ❓ Comprobación Final: ¿Cómo sé si todo está bien?

Abre la carpeta del proyecto en el Explorador de Windows. Debes ver algo como esto:

```text
📁 Facturas-Manager/
 ├── 📄 invoice_manager.py       (Código principal)
 ├── 📄 enviomedical_portal.py   (Descargador de EnvíoMédical)
 ├── 📄 requirements.txt         (Lista de librerías)
 ├── 📄 credentials.json         <-- TU ARCHIVO DE GOOGLE DRIVE (Aportado por ti)
 ├── 📄 .env                     <-- TU ARCHIVO DE CLAVES (Aportado por ti)
 ├── 📄 token.json               <-- GENERADO SOLO TRAS EL PRIMER USO
 └── 📄 supplier_aliases.json    (Opcional, tus alias de proveedores)
```

Si ejecutas `python invoice_manager.py` y en la pantalla ves:
```text
[INFO] Conectando a Gmail...
[INFO] Conexion establecida.
[INFO] Gemini inicializado...
[INFO] Google Drive autenticado.
[INFO] 0 correo(s) encontrado(s)...
```
**¡Enhorabuena! El sistema está al 100% restaurado y operativo.**
