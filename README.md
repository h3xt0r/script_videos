# script_videos

Pipeline para producir audiolibros largos en español (e inglés) a partir de EPUB
o PDF, con un LLM local en [Unsloth Studio](http://localhost:8888):
`Gemma-4-E4B` prepara el texto y `Qwen3-TTS-12Hz-1.7B` lo narra con una voz
clonada consistente.

```text
material ──► extract_epub.py ──┐
                                ├─► generar_texto_para_audio.py ─► bloques ─► generar_audiolibro.py ─► audiolibro.mp3
material ──► extract_pdf.py ───┘        (Gemma + visión)             (Qwen3-TTS)
```

## Directorios

| Carpeta | Qué hace |
| --- | --- |
| [`epub_extract/`](epub_extract/README.md) | Extrae EPUB y prepara el texto para narración: cifras a palabras, siglas, URLs fuera, descripción de diagramas y fórmulas, bloques de 15–20 min. |
| [`pdf_extract/`](pdf_extract/README.md) | Extrae PDF a **texto plano** (sin HTML): OCR selectivo por página, orden de lectura a dos columnas, marcas de fórmula y figuras incrustadas o vectoriales. |
| [`tts/`](tts/CATALOGO_VOCES.md) | Sintesis por fragmentos de hasta 250 palabras y unión de MP3 sin recodificar. Catálogo de voces cerrado. |

## Requisitos

- Unsloth Studio corriendo con `Gemma-4-E4B` (GGUF, con `mmproj` para visión) y
  `Qwen3-TTS-12Hz-1.7B-Base-GGUF`.
- Python 3.12 (`venv/` local, no versionado).
- Clave de Unsloth Studio en `UNSLOTH_API_KEY` o en `.unsloth.key` junto al script (ignorado).
- Para PDF: `poppler-utils`, `mupdf-tools`, `ocrmypdf` + `tesseract` (con `spa`
  y `eng`) e ImageMagick.

## Uso

```bash
# 1) Extraer
cd epub_extract && python extract_epub.py libro.epub      # o .pdf en pdf_extract
cd ../pdf_extract && python extract_pdf.py libro

# 2) Preparar el texto y partir en bloques
cd ../epub_extract && python generar_texto_para_audio.py --libro libro

# 3) Narrar
cd ../tts && python generar_audiolibro.py libro
```

## Qué no se sube

El repositorio contiene solo código y documentación. Material de lectura y sus
derivados quedan locales (ver `.gitignore`):

- `epub_extract/src/`, `pdf_extract/src/` y cualquier `.pdf`/`.epub` en la raíz:
  libros y artículos de entrada.
- `epub_extract/html/`, `epub_extract/preparado/`, `pdf_extract/texto/` y
  `tts/audiolibros/`: extracción, texto preparado, cachés y audio.
- Todo el `.mp3` y `.wav`, con dos excepciones versionadas porque definen las
  voces de producción: `tts/consistencia_b.mp3` (referencia español) y
  `tts/clon_narradora_english.mp3` (referencia inglés).

Las carpetas anteriores se conservan en el repositorio con un `.gitkeep` para
que el pipeline funcione recién clonado.

## Protección extra: hook pre-push

`.gitignore` deja de proteger un archivo en el momento en que se versiona (por
un `git add -f`, por ejemplo). Para eso está `.githooks/pre-push`, que revisa lo
que va a viajar al remoto y corta el push si aparece material privado, audio
derivado o claves. Las dos voces base están admitidas de forma explícita.

Hay que activarlo una vez por clon:

```bash
git config core.hooksPath .githooks
```

Se puede probar sin tocar el remoto con `git push --dry-run`. El hook revisa dos
cosas: el árbol final que se publica y cualquier archivo añadido en los commits
del rango, porque un archivo borrado más adelante sigue siendo accesible por SHA
en GitHub.
