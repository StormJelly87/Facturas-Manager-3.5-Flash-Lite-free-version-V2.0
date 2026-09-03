# 📜 Historial de Cambios (Changelog)

Todas las novedades, mejoras y correcciones notables de este proyecto están documentadas en este archivo.

El formato se basa en [Keep a Changelog](https://keepachangelog.com/es-ES/1.0.0/) y este proyecto se adhiere a [Semantic Versioning](https://semver.org/lang/es/).

---

## [2.0.0] - 2026-09-03

### 🚀 Novedades Principales
- **Actualización de IA a Gemini 3.5 Flash-Lite:**
  - Migración oficial al modelo `models/gemini-3.5-flash-lite`, reduciendo la latencia de respuesta y mejorando la precisión en la extracción de datos de facturas.
- **Fase 2: Portal B2B EnvíoMédical (`enviomedical_portal.py`):**
  - Implementación de un cliente web determinista con scraping automatizado sobre `env.titaniatools.es` (plataforma B2B de EnvíoMédical / Titania Tools).
  - Manejo automatizado de sesión HTTP, cookies de autenticación y tokens CSRF.
  - Consulta incremental de facturas mediante margen de seguridad configurable (`ENVIO_LOOKBACK_DAYS`, 45 días por defecto).
  - Estado local de sincronización en `enviomedical_state.json` para descargar únicamente documentos nuevos de forma idempotente.
  - Subida directa a Google Drive bajo la jerarquía `Año / MM-YY / ENVÍOMÉDICAL`.
- **Filtro automático de remitente `@enviomedical.com`:**
  - Los correos procedentes de EnvíoMédical se marcan automáticamente como leídos y se descartan en la Fase 1, evitando duplicados con la descarga directa del portal.
- **Sistema de Rescate por Huella Fiscal (*Fingerprinting Jaccard*):**
  - Módulo de comparación basado en similitud de Jaccard sobre palabras clave fiscales contra facturas de referencia en `Facturas ejemplo/`.
  - Si Gemini rechaza una factura real compleja, el sistema de huella fiscal la rescata deterministamente.
- **Rescate de Facturas Escaneadas:**
  - Re-evaluación visual multimodal para documentos sin texto digital OCR extraíble (tickets térmicos, escaneos físicos).
- **Mapeos Dinámicos de Proveedores (`supplier_aliases.json`):**
  - Desacoplamiento de nombres fiscales y CIFs personales a nombres comerciales a través de archivo de configuración externo, con plantilla `supplier_aliases.example.json`.

### 🛡️ Seguridad y Privacidad
- **Anonimización y Limpieza para Código Abierto:**
  - Eliminación de datos identificativos, correos corporativos y carpetas fijas de Google Drive del código fuente.
  - Purga total del historial de Git para permitir hacer el repositorio público con 100% de cumplimiento del RGPD.
  - Exclusión estricta de credenciales, secretos, facturas de muestra y mapeos privados en `.gitignore`.
- **Guía de Restauración para No Técnicos (`GUIA_RESTAURACION.md`):**
  - Manual paso a paso con GitHub Desktop para restaurar el sistema en un equipo nuevo desde cero sin necesidad de comandos de consola.

### 🐛 Correcciones y Mejoras
- **Soporte de Fechas Europeas Avanzadas:**
  - Detección de meses abreviados (ej. `27-Jun-2026`) y expresiones textuales en español (`6 de marzo`).
  - Filtro de seguridad anti-fechas futuras que invierte automáticamente día y mes cuando la IA confunde formatos DD/MM y MM/DD.
- **Detección de Duplicados en Google Drive:**
  - Verificación por hash MD5 en subidas: no re-sube archivos idénticos y añade sufijos correlativos `(1)` solo si el contenido difiere.

---

## [1.2.0] - 2026-04-03

### 🐛 Correcciones
- Mejora de expresiones regulares y filtros para rechazar tickets sin fecha válida.
- Prevención de alucinaciones en modelos de IA con reglas estrictas de exclusión para documentos que no son facturas (albaranes, presupuestos, confirmaciones de pedido).
- Regla de rechazo preventivo para números de factura web que comiencen por "FW".

---

## [1.1.0] - 2026-03-18

### 🚀 Novedades
- **Gestión de Rate Limits:** Pausas de cortesía y reintentos automáticos con backoff exponencial ante errores HTTP 429.
- **Normalización de Proveedores en Drive:**
  - Algoritmo de matching inteligente (exacto, parcial y por palabras clave).
  - Eliminación automática de sufijos societarios (`S.L.`, `S.A.`, `LTD`, etc.) y normalización de acentos para evitar carpetas duplicadas.
- **Regla Prioritaria Amazon:** Detección automática por dominio `@amazon.*` para unificar compras bajo la carpeta `AMAZON`.

---

## [1.0.0] - 2026-02-15

### 🚀 Versión Inicial
- Conexión desatendida a Gmail mediante protocolo IMAP con contraseñas de aplicación.
- Búsqueda automatizada de correos no leídos con adjuntos (`is:unread has:attachment factura`).
- Clasificación de adjuntos multimodales (PDFs e imágenes) mediante la API de Gemini.
- Creación automatizada de la jerarquía de carpetas en Google Drive (`Año / MM-YY / PROVEEDOR`).
- Marcado de correos como leídos (`\Seen`) tras subida exitosa.
