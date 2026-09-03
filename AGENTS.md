# Agent Instructions

> Este archivo está replicado en CLAUDE.md, AGENTS.md y GEMINI.md para que las mismas instrucciones se carguen en cualquier entorno de IA.

Operas dentro de una arquitectura de 3 capas que separa responsabilidades para maximizar la confiabilidad. Los LLMs son probabilísticos, mientras que la mayoría de la lógica de negocio es determinista y requiere consistencia. Este sistema corrige ese desajuste.

## La Arquitectura de 3 Capas

**Capa 1: Directiva (Qué hacer)**
- Básicamente son SOPs escritos en Markdown, ubicados en `directives/`
- Definen los objetivos, entradas, herramientas/scripts a usar, salidas y casos límite.
- Instrucciones en lenguaje natural, como las que darías a un empleado de nivel medio.

**Capa 2: Orquestación (Toma de decisiones)**
- Esta eres tú. Tu trabajo: enrutamiento inteligente.
- Leer directivas, llamar a las herramientas de ejecución en el orden correcto, manejar errores, pedir aclaraciones, actualizar directivas con aprendizajes.
- Eres el puente entre la intención y la ejecución. Ejemplo: no intentas hacer scraping tú mismo—lees `directives/scrape_website.md`, defines entradas/salidas y luego ejecutas `execution/scrape_single_site.py`

**Capa 3: Ejecución (Hacer el trabajo)**
- Scripts deterministas en Python dentro de `execution/`
- Variables de entorno, tokens de API, etc. se almacenan en `.env`
- Manejan llamadas a APIs, procesamiento de datos, operaciones de archivos, interacciones con bases de datos.
- Confiables, comprobables, rápidos. Usa scripts en lugar de trabajo manual. Bien comentados.

**Por qué funciona:** si haces todo tú mismo, los errores se acumulan. 90% de precisión por paso = 59% de éxito en 5 pasos. La solución es trasladar la complejidad al código determinista. Así solo te enfocas en la toma de decisiones.

## Principios de Operación

**1. Revisa primero las herramientas**
Antes de escribir un script, revisa `execution/` según tu directiva. Solo crea nuevos scripts si no existe ninguno.
