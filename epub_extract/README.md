# epub_extract

Extrae EPUB a una carpeta por libro y prepara el texto para convertirlo en
audiolibro con un LLM local. El mismo preparador acepta los libros que salida de
[`../pdf_extract`](../pdf_extract): no hay que mover nada, solo indicar el libro.

## Estructura

```text
epub_extract/
├── extract_epub.py              # extrae los .epub
├── generar_texto_para_audio.py  # limpia el texto y crea los bloques (EPUB y PDF)
├── src/                         # aquí van los .epub originales
├── html/
│   └── SOME_ARCHIVE/            # extracción de SOME_ARCHIVE.epub
│       ├── mimetype
│       ├── META-INF/
│       └── OEBPS/
│           ├── content.opf
│           ├── chapter1.xhtml
│           ├── style.css
│           └── images/
└── preparado/
    ├── cache/                   # fragmentos ya preparados (SHA-256)
    ├── cache_imagenes/          # descripciones de figuras hechas con visión
    └── SOME_ARCHIVE/
        ├── bloque_01.txt
        ├── bloque_02.txt
        ├── texto_completo.txt
        └── manifiesto.json
```

## Extraer un EPUB

```bash
cd /home/kritt/Git/script_videos/epub_extract

# un solo libro
python extract_epub.py SOME_ARCHIVE.epub

# todos los .epub de src/
python extract_epub.py

# reemplazar una extracción existente
python extract_epub.py SOME_ARCHIVE.epub --force
```

También acepta el nombre sin extensión: `python extract_epub.py SOME_ARCHIVE`.

Qué hace:

- Descomprime todos los elementos del EPUB sin aplanar directorios, para que los
  enlaces relativos entre XHTML, CSS e imágenes sigan funcionando.
- Ignora basura de sistemas operativos: `__MACOSX`, `.DS_Store`, `Thumbs.db`.
- Lee `META-INF/container.xml` e imprime la ruta del `.opf` (el manifiesto).
- Muestra un resumen por tipo de archivo y el tamaño extraído.
- Rechaza entradas con rutas absolutas o `..` (zip slip).
- No sobrescribe una extracción previa salvo que se use `--force`.

## Preparar el texto para audio

Requiere Unsloth Studio en `http://localhost:8888` con `Gemma-4-E4B` cargado.
El script lo carga si hace falta, con 131,072 tokens de contexto y KV cache
`q8_0` (unos 4 GB de VRAM). También conviene tener libre el TTS si se va a
generar el audio después.

> `src/`, `html/` y `preparado/` están en `.gitignore`: el material de lectura y
> sus derivados no se suben a GitHub. Solo viaja el código.

```bash
cd /home/kritt/Git/script_videos/epub_extract

# el único libro extraído
python generar_texto_para_audio.py

# elegir libro (nombre exacto o parcial)
python generar_texto_para_audio.py --libro "SOME_ARCHIVE"

# prueba con el primer capítulo
python generar_texto_para_audio.py --libro "SOME_ARCHIVE" --limite 1

# ajustar duración y velocidad
python generar_texto_para_audio.py --libro "SOME_ARCHIVE" --minutos 18 --wpm 160

# no cargar el modelo; falla si no está listo
python generar_texto_para_audio.py --libro "SOME_ARCHIVE" --sin-cargar
```

Parámetros:

| Parámetro | Defecto | Descripción |
| --- | --- | --- |
| `--libro` | autodetecta | Carpeta dentro de `html/` o de `../pdf_extract/texto/`. Admite coincidencia parcial. |
| `--fuente` | `auto` | `epub`, `pdf` o `auto` (busca EPUB primero). |
| `--limite` | todos | Procesa solo los primeros N capítulos o páginas. |
| `--minutos` | 18 | Minutos objetivo por bloque. |
| `--wpm` | 160 | Palabras por minuto del TTS. Medido por fragmento en texto técnico (140–176); una narración simple ronda 175. |
| `--sin-cargar` | — | No intenta cargar Gemma. |
| `--sin-vision` | — | En PDF no describe las figuras: se quitan las marcas `<<<IMAGEN>>`. |

Qué hace:

1. Lee el OPF y recorre el spine en orden de lectura, ignorando documentos
   `linear="no"` y el índice de navegación.
2. Extrae párrafos, encabezados, listas y tablas del XHTML. Marca diagramas
   (`<pre>`), fórmulas (`<math>`) e imágenes para que el LLM los describa en
   lugar de leer código o símbolos.
3. Expande monedas y porcentajes de forma determinista en Python antes de
   llamar al LLM. Así `$184,019M` se convierte en «ciento ochenta y cuatro mil
   diecinueve millones de dólares» y `20.7%` en «veinte coma siete por ciento»,
   sin depender de la aritmética del modelo.
4. Agrupa el texto en fragmentos de hasta 3,500 palabras y pide a Gemma que lo
   deje listo para narración: números a palabras, abreviaturas, siglas, URLs
   fuera y descripción natural de diagramas y fórmulas. No resume.
5. Cachea cada fragmento en `preparado/cache/` usando su SHA-256. Repetir una
   ejecución no vuelve a llamar al LLM para el mismo texto. La caché guarda la
   salida cruda del modelo; la normalización final (encabezados, dominios,
   variables, contactos) se aplica al leer, así que ajustar esa normalización no
   obliga a regenerar el texto.
6. Reparte los párrafos en bloques equilibrados de 15 a 20 minutos
   (18 min objetivo), prefiriendo cortar en un cambio de capítulo. Si el total
   del libro no permite un reparto exacto, lo indica con un aviso.
   El valor por defecto es `--wpm 160`, medido sobre los fragmentos de un bloque
   técnico real (140–176 ppm, promedio 160); para prosa narrativa simple el ritmo
   ronda 175 ppm. Después de generar el primer audio,
   `generar_audiolibro.py` imprime el ritmo real por bloque.

Salida:

- `bloque_NN.txt`: texto listo para pasar al TTS, sin marcas ni Markdown.
- `texto_completo.txt`: todos los bloques concatenados.
- `manifiesto.json`: palabras, minutos estimados, capítulos y parámetros usados.

La caché incluye `VERSION_PROMPT`; al cambiar el prompt o el modelo, sube esa
constante para invalidar los fragmentos anteriores.

## Usar libros en PDF

`../pdf_extract` deja cada libro en `pdf_extract/texto/<libro>/texto.txt` y ese
preparador lo lee sin pasos intermedios:

```bash
cd /home/kritt/Git/script_videos/pdf_extract
python extract_pdf.py MiLibro.pdf --force

cd ../epub_extract
python generar_texto_para_audio.py --libro MiLibro --fuente pdf
```

Diferencias frente al EPUB:

- El origen es **texto plano**, sin HTML: las etiquetas de `pdftotext
  -bbox-layout` solo se usan aquí, para reconstruir párrafos y columnas, y no
  llegan al LLM. Así no se gastan tokens en marcas.
- Las unidades de lectura son páginas (`página 1`, `página 2`, …) en lugar de
  capítulos del spine.
- Las marcas `<<<IMAGEN: imagenes/…>>>` del PDF se resuelven con la parte de
  visión de `Gemma-4-E4B` (`mmproj`) antes de preparar el fragmento: cada figura
  se describe una vez y se cachea en `preparado/cache_imagenes/` por SHA-256 de
  la imagen. Las marcas `<<<FORMULA: …>>>` se leen en voz alta con las reglas
  ya existentes del prompt.
- `manifiesto.json` del libro registra `"origen": "pdf"` además de las palabras,
  los minutos y los bloques.

Para probar el flujo completo sin descripción de figuras usa `--sin-vision`.
