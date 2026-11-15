# pdf_extract

Extrae PDF a **texto plano**, sin HTML, y deja el material listo para que
`epub_extract/generar_texto_para_audio.py` lo prepare para narración.

## Por qué texto plano y no HTML

`pdftohtml` y las conversiones intermedias llenan el texto de `<div>`, `<span>`
y coordenadas que el LLM paga en tokens sin aportar contenido: en un libro de
40,000 palabras son miles de líneas de etiquetas. Aquí el XML de
`pdftotext -bbox-layout` se usa **solo en local**, para reconstruir párrafos,
columnas, fórmulas y figuras; lo que se guarda en disco es prosa limpia. Con
esto:

- bajan los tokens por llamada a Gemma y el costo de cada fragmento;
- el modelo no tiene etiquetas que copiar al texto final;
- se conserva el orden de lectura real (columnas, pies de figura), no el orden
  del marcado;
- lo no narrable se señala con marcas cortas (`<<<IMAGEN: ...>>>`,
  `<<<FORMULA: ...>>>`) que la preparación ya sabe convertir en prosa.

## Estructura

```text
pdf_extract/
├── extract_pdf.py        # extrae los .pdf
├── README.md
├── src/                  # aquí van los .pdf originales
└── texto/
    └── SOME_BOOK/
        ├── texto.txt         # texto plano; páginas separadas por \f
        ├── manifiesto.json   # páginas, motor, OCR, figuras
        └── imagenes/         # figuras extraídas o recortadas (si hay)
```

## Extraer un PDF

```bash
cd /home/kritt/Git/script_videos/pdf_extract

python extract_pdf.py SOME_BOOK.pdf      # un libro
python extract_pdf.py                    # todos los .pdf de src/
python extract_pdf.py SOME_BOOK --force  # reemplaza la extracción previa
python extract_pdf.py SOME_BOOK --sin-ocr       # no intenta OCR
python extract_pdf.py SOME_BOOK --sin-imagenes  # no extrae figuras
```

Acepta ruta completa o nombre dentro de `src/`, con o sin extensión.

> `src/` y `texto/` están en `.gitignore`: los libros y el texto extraído se
> quedan locales y solo se versiona el código.

## Qué hace

1. **Texto.** `pdftotext -enc UTF-8`; si no estuviera, cae a `mutool draw -F txt`.
2. **OCR selectivo.** Cuenta los caracteres alfanuméricos de cada página; las
   que bajan de 40 se consideran escaneadas y se procesan con
   `ocrmypdf --force-ocr --pages <rango> --deskew --optimize 0` en los idiomas
   que tenga Tesseract (`spa+eng`). Al limitar el rango a las páginas sin capa
   de texto, ninguna página legible se reconoce otra vez ni se duplica.
3. **Estructura.** `pdftotext -bbox-layout` da bloques y líneas con coordenadas
   (ese XML no se guarda). Con ellas:
   - se quitan encabezados, pies y números de página que se repiten en la mitad
     o más de las páginas (con dos páginas basta que aparezca en ambas);
   - se unen las palabras cortadas por guion;
   - se detectan **columnas** por el borde izquierdo de los bloques y se lee
     columna por columna de arriba abajo; los bloques de ancho completo abren y
     cierran bandas, así el título de una sección no se cuela dentro del
     diagrama que está al lado;
   - los párrafos se cortan por sangría o por salto vertical grande.
4. **Fórmulas.** Una ecuación centrada sale como varios bloques sueltos
   (`T =`, `I`, `OE`, `(1)`). Las piezas cercanas se reúnen y se emite
   `<<<FORMULA: T = I / OE>>>`, con numerador y denominador unidos por `/`; el
   número de ecuación se descarta.
5. **Figuras.**
   - *Incrustadas:* `pdfimages -list` y `pdfimages -png`. Se descartan iconos
     (menos de 80 px) y páginas escaneadas completas (80 % de la hoja o más),
     cuyo contenido ya aporta el OCR. Se guardan como JPEG de lado ≤1600 px y
     calidad 90, deduplicadas por SHA-256.
   - *Vectoriales* (TikZ, diagramas, gráficos dibujados): `pdfimages` no las
     ve. Si la página tiene una leyenda `Figura 1:` y no hay imagen incrustada,
     se renderiza con `pdftoppm -r 150` la franja que va del último párrafo a la
     leyenda, se recorta el margen blanco con ImageMagick y se guarda como
     `pagina_NNN_figura.jpg`. Las etiquetas sueltas del diagrama se quitan del
     texto: la descripción con visión las sustituye.
   - Cada figura se inserta con `<<<IMAGEN: imagenes/…>>>` justo antes de su
     leyenda.
6. **Salida.** `texto.txt` (páginas separadas por `\f`, párrafos por línea en
   blanco) y `manifiesto.json` con páginas, motor usado, páginas con OCR y las
   figuras con su SHA-256.

## Preparar el texto y el audio

```bash
cd /home/kritt/Git/script_videos/pdf_extract
python extract_pdf.py MiLibro --force

cd ../epub_extract
python generar_texto_para_audio.py --libro MiLibro --fuente pdf

cd ../tts
python generar_audiolibro.py MiLibro
```

La preparación usa la parte de visión de `Gemma-4-E4B` (`mmproj`) para
describir cada `<<<IMAGEN: …>>>` antes de limpiar el fragmento; la descripción
se cachea en `epub_extract/preparado/cache_imagenes/` por SHA-256 de la imagen,
así que una figura no se describe dos veces ni siquiera entre libros. Con
`--sin-vision` las marcas de imagen se eliminan en lugar de describirse.

## Herramientas que usa

`poppler-utils` (`pdftotext`, `pdfinfo`, `pdfimages`, `pdftoppm`),
`mupdf-tools` (`mutool`, respaldo de texto), `ocrmypdf` con `tesseract`
(`spa`, `eng`, `osd`) para escaneados e ImageMagick (`convert`) para
recomprimir y recortar. No se guarda ningún intermediario HTML ni OCR en disco:
el PDF con capa de texto generada por `ocrmypdf` vive en un temporal que se
borra al terminar.

## Notas

- Los libros origen PDF se preparan en unidades por página (`página 1`,
  `página 2`, …); un PDF no declara capítulos, así que el reparto en bloques de
  15–20 minutos corta en cambio de página cuando queda cerca del punto ideal.
- Un PDF con solo 2 páginas conserva los encabezados repetidos si aparecen en
  ambas; el umbral es de 50 % de las páginas.
- El ritmo de narración calibrado es de 160 palabras por minuto (140–176 en
  texto técnico), el mismo que para EPUB.
