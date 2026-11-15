Objective
- Producir audiolibros largos en español con Qwen3-TTS en Unsloth Studio, usando MP3 original sin recomprimir y una voz clonada consistente.
- Preparar texto de EPUB y de PDF con Gemma-4-E4B: limpieza para lectura, descripción de diagramas/fórmulas/figuras y división en bloques de 15–20 minutos.
Requirements
- Usar la salida MP3 original del TTS; no recomprimir para producción.
- Código limpio: constantes de conexión arriba y parámetros en payload.
- En la API de Unsloth el campo correcto es instructions; instruct pertenece a qwen-tts y se ignora.
- Catálogo de voces cerrado: español narradora_consistencia_b; inglés nativo separado narradora_english; tono neutro y profesional.
- El LLM debe preparar el texto para lectura y describir diagramas y/o fórmulas.
- Dividir el texto en bloques para audio de unos 15 a 20 minutos.
- Herramienta que extraiga texto de PDF desde pdf_extract/src/, usando pdftotext, mupdf-tools y/o ocrmypdf.
- Texto plano, sin HTML, para no gastar tokens de Gemma-4-E4B en etiquetas.
- Aprovechar la parte de visión de Gemma-4-E4B (mmproj) para las figuras del PDF.
Decisions
- Usar Unsloth Studio + GGUF: menor VRAM, sin PyTorch; Qwen3-TTS RTF variable.
- Modelo TTS de clonación: Qwen3-TTS-12Hz-1.7B-Base-GGUF; VoiceDesign solo para crear referencias.
- Flujo TTS: VoiceDesign → referencia limpia → clonar con Base → usar la voz clonada en todo el libro.
- seed=12345; reproducible por voz, no iguala voces entre idiomas.
- Usar Gemma-4-E4B (Q4_K_M) para preparar texto, con max_seq_length: 131072 y cache_type_kv: "q8_0".
- Desactivar razonamiento con chat_template_kwargs: {"enable_thinking": false}.
- Expandir monedas y porcentajes de forma determinista en Python antes del LLM.
- Calibración corregida: 160 ppm (rango real 140–176 por fragmento); el valor anterior de 120 ppm salió de un audio corrupto.
- Objetivo 18 min por bloque, rango 15–20.
- Fragmentos de hasta 3500 palabras por llamada al LLM y max_tokens: 10240.
- Para TTS: dividir cada bloque en fragmentos de máximo 250 palabras por petición y unir sus MP3 con ffmpeg -c copy.
- extractor de PDF en su propio directorio pdf_extract/ (no dentro de epub_extract/).
- Salida del PDF: texto/<libro>/texto.txt (páginas separadas por \f) + manifiesto.json + imagenes/.
- El HTML de pdftotext -bbox-layout se usa solo en memoria, para orden de columnas, párrafos y zonas de figura; nunca se guarda ni llega al LLM.
- OCR con ocrmypdf --force-ocr --pages <rango>, limitado a las páginas sin capa de texto (menos de 40 caracteres alfanuméricos); nada de --skip-text global.
- Figuras incrustadas con pdfimages; figuras vectoriales renderizando la franja con pdftoppm -r 150 + convert -trim cuando hay leyenda «Figura N:» y no hay imagen incrustada.
- La clave de Unsloth Studio no se escribe en el código: se lee de UNSLOTH_API_KEY o de .unsloth.key (0600, ignorado por git) junto a cada script.
- Repo público h3xt0r/script_videos recreado desde cero con un solo commit (90192a6): sin libros de muestra, sin texto extraído y sin la clave. src/, html/, preparado/, texto/ y audiolibros/ quedan fuera; solo viajan las voces base consistency_b.mp3 y clon_narradora_english.mp3.
- Hook .githooks/pre-push (activar con git config core.hooksPath .githooks): corta el push si sube material privado, audio derivado o claves.
- Las etiquetas de un diagrama vectorial se quitan del texto: las sustituye la descripción con visión.
- Marcas <<<IMAGEN: imagenes/...>>> y <<<FORMULA: ...>>> en el texto plano; la preparación resuelve las rutas con Gemma visión y cachea por SHA-256 en preparado/cache_imagenes/.
Work State
Completed
- Catálogo TTS cerrado y verificado: narradora_consistencia_b (ee3a67f717aa4484be290a666182366d) y narradora_english (b1e61e64ef8d4eeaaf1d93ee67c7dc2f).
- epub_extract/ con extract_epub.py, src/, html/, preparado/ y README.md; generar_texto_para_audio.py completo (OPF/spine, marcas, expansión numérica, limpieza con Gemma, caché cruda, bloques).
- Audiolibro real del EPUB verificado: 8 fragmentos, 10.5 min, PCM MD5 idéntico, picos 0.61, similitud de transcripción 0.95.
- Gemma-4-E4B confirmado con visión: check-vision devuelve is_vision true y /v1/chat/completions acepta content multimodal con image_url en data URI.
- pdf_extract/ creado con extract_pdf.py y README.md: pdftotext + respaldo mutool, OCR selectivo por página, limpieza de encabezados/pies/números, detección de columnas por borde izquierdo de bloque, bandas de ancho completo, unión de líneas cortadas, marcas de fórmula y extracción de figuras (raster y vectoriales).
- Probado con cuatro casos: prueba_texto (2 páginas, fórmula, figura PNG incrustada), prueba_dos_columnas (orden por columna correcto), escaneado (OCR 2 páginas, sin duplicar páginas como figuras) y prueba_mixto (página 1 con texto + 2 escaneadas → OCR solo en 2 y 3).
- Boletin-07-Politicas-Economicas-bajo-TOC.pdf extraído: 4 páginas, 738 palabras, 1 figura vectorial recortada (928x1120) y descrita con visión.
- generar_texto_para_audio.py integrado con PDF: elegir_fuente(), leer_texto_plano(), describir_imagen() con caché por SHA-256, resolver_imagenes(), --fuente auto/epub/pdf y --sin-vision; manifiesto registra "origen".
- Caché de descripciones de figuras en preparado/cache_imagenes/ y VERSION_IMAGEN = "3"; VERSION_PROMPT sigue en "5".
- Limpieza añadida: marcas de Markdown (* _ `) fuera de la salida del LLM y de las descripciones; enumeraciones dentro de la línea («. 2. Explotar») convertidas a palabras.
- Flujo PDF → bloques → audio validado: Boletin-07 PDF, 822 palabras, 5 fragmentos, 5.2 min, 159 ppm real, pico 0.463, sin saltos anómalos (solo 4 s y 175 s con delta ≤0.19).
- README.md de pdf_extract/ y sección PDF en epub_extract/README.md (incluye el porqué del texto plano).
Active
- Nada pendiente del flujo PDF; queda pendiente auditar libros PDF largos (varias centenas de páginas) para confirmar el umbral de encabezados repetidos y el recorte de figuras vectoriales.
Blocked
- None.
Next Move
1. Ejecutar el flujo PDF con un libro largo real (300+ páginas) y revisar reparto de bloques y recortes de figuras.
2. Si aparecen figuras sin leyenda, considerar recortar también las zonas sin «Figura N:» o detectar bloques de solo símbolos.
3. Opcional: en PDF, agrupar páginas en capítulos detectando títulos cortos para que los cortes de bloque caigan en secciones y no en páginas.
Relevant Files
- /home/kritt/Git/script_videos/pdf_extract/extract_pdf.py: extractor PDF en texto plano (OCR, columnas, fórmulas, figuras).
- /home/kritt/Git/script_videos/pdf_extract/README.md: por qué texto plano, opciones de CLI y qué hace cada etapa.
- /home/kritt/Git/script_videos/pdf_extract/src/: Boletin-07 real, escaneado.pdf, prueba_texto.pdf, prueba_dos_columnas.pdf, prueba_mixto.pdf.
- /home/kritt/Git/script_videos/pdf_extract/texto/<libro>/texto.txt + manifiesto.json (+ imagenes/): salida del extractor.
- /home/kritt/Git/script_videos/epub_extract/generar_texto_para_audio.py: elegir_fuente() ~línea 415, leer_texto_plano() ~línea 455, describir_imagen()/resolver_imagenes() ~línea 897, main() ~línea 1230.
- /home/kritt/Git/script_videos/epub_extract/preparado/cache_imagenes/: descripciones de figuras por SHA-256.
- /home/kritt/Git/script_videos/epub_extract/README.md: flujo EPUB, tabla de parámetros y sección «Usar libros en PDF».
- /home/kritt/Git/script_videos/tts/generar_audiolibro.py: fragmentos ≤250 palabras, reanudación, liberación de VRAM y unión sin recodificar.
- /home/kritt/Git/script_videos/tts/audiolibros/Boletin-07-Politicas-Economicas-bajo-TOC/audiolibro.mp3: audio generado desde un PDF (5.2 min).
- /home/kritt/Git/script_videos/venv: Python del proyecto (numpy 2.5.4, faster-whisper 1.2.1 para diagnóstico).
Important Context
- Servidor: http://localhost:8888. La clave va en `UNSLOTH_API_KEY` o en el archivo local `.unsloth.key` junto al script (ignorado por git).
- Modelo de texto: Gemma-4-E4B; carga: {"model_path":"Gemma-4-E4B","max_seq_length":131072,"cache_type_kv":"q8_0"}.
- Con parallel_slots: 4, el contexto efectivo por petición es ~32k tokens.
- Gemma razona: sin enable_thinking: false, un max_tokens bajo devuelve content vacío.
- Rendimiento de Gemma: ~108 tok/s; fragmento de 1650 palabras en ~3–13 s; una figura en ~2 s.
- Visión: messages[].content como lista con {"type":"text"} y {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,..."}}.
- El TTS degenera si una petición supera ~170 s de audio (2,048 frames a 12 Hz); por eso el límite de 250 palabras.
- Calibración real: 160 ppm promedio (140–176 por fragmento); 1,679 palabras → 10.5 min.
- Cargar Gemma y el TTS a la vez puede fallar por VRAM; ambos scripts liberan automáticamente el otro modelo si reciben out_of_memory.
- El MP3 del TTS viene a 24 kHz, mono, 32 kbps; la unión con ffmpeg -c copy conserva el PCM idéntico.
- ocrmypdf --force-ocr --pages funciona por rango de páginas; Tesseract tiene spa, eng y osd. reportlab, fpdf e img2pdf no están instalados.
- Para fabricar PDFs de prueba: pdflatex para texto y columnas; convert page-N.png escaneado.pdf para simular escaneo; gs -sDEVICE=pdfwrite para mezclar páginas de texto y escaneadas (mutool merge rompe la codificación de fuentes de LaTeX).
- pdfimages -list da página, número, tamaño y ppi; pdfimages -f N -l N -png -p nombra como prefijo-001-000.png.
- Las figuras que cubren ≥80 % de la página se descartan para no reenviar páginas escaneadas completas a visión.
- pdftotext -bbox-layout pone cada columna en un <flow> distinto; el orden por y intercala títulos de columnas, de ahí la ordenación por columna en extract_pdf.py.
- La preparación de un PDF trata cada página como unidad de lectura; los bloques de audio cortan preferentemente en cambio de página.
