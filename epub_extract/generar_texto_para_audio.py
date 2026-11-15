"""Prepara un EPUB extraído para narración con un LLM local.

Lee el OPF, extrae el texto en orden de lectura, pide a Gemma que lo deje listo
para leerse en voz alta (números, abreviaturas, diagramas y fórmulas) y agrupa
el resultado en bloques de 15 a 20 minutos de audio.

Uso:
    python generar_texto_para_audio.py
    python generar_texto_para_audio.py --libro "2026-11-12-Boletin-09: Deficit comercial mex-usa"
    python generar_texto_para_audio.py --libro ... --limite 1
    python generar_texto_para_audio.py --libro ... --minutos 18 --wpm 180

Salida:
    preparado/<libro>/bloque_01.txt
    preparado/<libro>/bloque_02.txt
    preparado/<libro>/texto_completo.txt
    preparado/<libro>/manifiesto.json
"""

from __future__ import annotations

import argparse
import base64
import bisect
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

import os
import requests

# --- Conexión con Unsloth Studio ---
BASE = os.environ.get("UNSLOTH_BASE", "http://localhost:8888")
# La clave no se versiona: va en UNSLOTH_API_KEY o en .unsloth.key junto al script.
KEY = os.environ.get("UNSLOTH_API_KEY", "").strip()
if not KEY:
    _archivo_clave = Path(__file__).resolve().parent / ".unsloth.key"
    KEY = _archivo_clave.read_text(encoding="utf-8").strip() if _archivo_clave.is_file() else ""
MODELO = "Gemma-4-E4B"
MAX_SEQ_LENGTH = 131072
KV_CACHE = "q8_0"

# --- Estructura del proyecto ---
RAIZ = Path(__file__).resolve().parent
HTML_DIR = RAIZ / "html"
SALIDA_DIR = RAIZ / "preparado"
# Los PDF se extraen con pdf_extract/extract_pdf.py y se leen desde aquí.
PDF_DIR = RAIZ.parent / "pdf_extract" / "texto"
CACHE_IMAGENES_DIR = SALIDA_DIR / "cache_imagenes"

# --- Parámetros de preparación ---
# 160 ppm: medido sobre los ocho fragmentos del bloque técnico real (140-176 ppm,
# promedio 160). El valor anterior de 120 salió de una síntesis corrupta que
# incluía minutos de ruido. La prosa narrativa simple ronda 175 ppm.
PALABRAS_POR_MINUTO = 160
MINUTOS_OBJETIVO = 18
MINUTOS_MIN = 15
MINUTOS_MAX = 20

# Con 131k de contexto, un bloque completo de audio cabe en una sola llamada.
PALABRAS_POR_LLAMADA = 3500
MAX_TOKENS = 10240
TEMPERATURA = 0.0
REINTENTOS = 3
MAX_CARACTERES_VISUAL = 8000

# --- Parámetros de visión (figuras de PDF) ---
MAX_TOKENS_IMAGEN = 512
# Cambiar esta versión invalida la caché de descripciones de figuras.
VERSION_IMAGEN = "3"
TIPOS_IMAGEN = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "webp": "image/webp", "gif": "image/gif", "tif": "image/tiff",
                "tiff": "image/tiff"}

# Cambiar la versión invalida la caché de fragmentos ya preparados.
VERSION_PROMPT = "5"

SISTEMA = """Eres un editor profesional de audiolibros. Recibes un fragmento de un libro \
y devuelves el mismo fragmento preparado para ser leído en voz alta por un narrador. \
Trabajas en el idioma original del texto.

REGLAS OBLIGATORIAS:
1. Conserva todo el contenido, el orden y el sentido. No resumas, no omitas párrafos \
ni añadas información que no esté en el texto.
2. Los números, fechas, porcentajes y monedas pueden aparecer ya escritos en palabras. \
Consérvalos exactamente como están; no los reconviertas a dígitos ni alteres su valor. Si \
queda alguna cifra, escríbela como se pronuncia. Ejemplo: «siglo XXI» se convierte en \
«siglo veintiuno»; «EE. UU.» en «Estados Unidos».
3. Las siglas que se leen como palabras se dejan tal cual (TOC, MMT). Las que se \
deletrean se escriben separadas por espacios solo si el contexto lo pide.
4. Mantén los términos técnicos y los nombres propios. Si un término inglés es de uso \
común (nearshoring, throughput), déjalo, pero intégralo en una frase natural.
5. No leas URLs, rutas, código ni marcas de formato. Si aparece un enlace, conserva \
solo su texto visible. Elimina las líneas que solo contienen un correo electrónico o una \
dirección de contacto.
6. Los bloques delimitados por <<<ELEMENTO_VISUAL>>> y <<<FIN_ELEMENTO_VISUAL>>> \
contienen el código de un diagrama, tabla o figura. NO leas el código: sustitúyelo por \
una descripción breve, de una a tres frases, de lo que muestra, en el punto exacto donde \
aparece y usando el contexto. No digas «en la imagen se ve»; descríbelo como parte de la \
narración.
7. Las marcas <<<FORMULA: ...>>> representan expresiones matemáticas. Léelas de forma \
natural dentro de la frase; no deletrees símbolos. Ejemplo: «(I y OE)» se convierte en \
«la inversión y el gasto de operación». Si el texto define una variable junto a su nombre \
(«Throughput (T)», «Inversión (I)»), omite la letra entre paréntesis y conserva solo el \
nombre. En una lista de variables, usa los nombres que el propio texto ya explicó; solo \
di «la variable T» cuando no haya ninguna pista.
8. Las marcas <<<IMAGEN: ...>>> contienen el texto alternativo de una imagen. Descríbela \
en una frase natural; no menciones que es una imagen.
9. Conserva los saltos de párrafo. No uses Markdown, viñetas, encabezados con \
almohadillas ni negritas.
10. Devuelve únicamente el texto final, sin comentarios, sin títulos añadidos y sin \
frases como «Aquí tienes el texto».
11. Si el fragmento empieza o termina a mitad de una idea, no lo completes ni lo \
resumas: limita su preparación a lo que está presente.
12. No cambies el registro ni el estilo del autor. Corrige solo artefactos de \
extracción: espacios raros, guiones de corte y caracteres mal codificados."""

REFUERZO = (
    "\n\nRECORDATORIO: no resumas. Conserva todas las ideas y todos los párrafos. "
    "Responde solo con el texto final preparado."
)

# --- Descripción de figuras con la parte de visión de Gemma (mmproj) ---
SISTEMA_IMAGEN = """Eres un describidor de figuras para audiolibros en español. \
Recibes una imagen y devuelves solo la descripción lista para narrarse.

REGLAS:
1. Describe qué muestra la figura y qué relación tiene con el texto: tipo de diagrama, \
elementos, flechas o conexiones y los rótulos o cifras que se lean en ella.
2. Escribe tres o cuatro frases seguidas, en prosa natural, sin listas y sin Markdown.
3. No digas «en la imagen», «se ve» ni «la figura muestra». Empieza directamente con el contenido.
4. No inventes datos que no aparezcan. Si algo no se lee, omítelo.
5. No añadas saludos, títulos ni explicaciones sobre la descripción."""

ENCABEZADOS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOQUES = {
    "p", "li", "blockquote", "figcaption", "dt", "dd", "caption", "aside",
} | ENCABEZADOS
SALTAR = {"head", "script", "style", "nav", "svg"}


@dataclass
class Parrafo:
    texto: str
    capitulo: str


# ---------------------------------------------------------------------------
# Extracción de XHTML
# ---------------------------------------------------------------------------


class ExtractorTexto(HTMLParser):
    """Convierte un XHTML en párrafos, con marcas para elementos no textuales."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.bloques: list[str] = []
        self._buffer: list[str] = []
        self._saltar = 0
        self._pre: list[str] | None = None
        self._math: list[str] | None = None
        self._annotation: list[str] | None = None
        self._annotation_cerrada = False
        self._titulo: str | None = None

    def _texto_buffer(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._buffer)).strip()

    def _flush(self) -> str:
        texto = self._texto_buffer()
        if texto:
            self.bloques.append(texto)
        self._buffer = []
        return texto

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SALTAR:
            self._saltar += 1
            return
        if self._saltar:
            return

        if tag == "pre":
            self._flush()
            self._pre = []
            return

        if tag == "math":
            self._math = []
            self._annotation = None
            self._annotation_cerrada = False
            return

        if tag == "annotation" and self._math is not None:
            self._annotation = []
            return

        if tag in BLOQUES:
            self._flush()
            return

        if tag == "br":
            self._buffer.append(" ")
        elif tag == "tr":
            self._flush()
        elif tag in ("td", "th") and self._texto_buffer():
            self._buffer.append(" · ")
        elif tag == "img":
            atributos = dict(attrs)
            alt = (atributos.get("alt") or "").strip()
            if alt:
                self._buffer.append(f" <<<IMAGEN: {alt[:500]}>>> ")

    def handle_endtag(self, tag: str) -> None:
        if tag in SALTAR:
            if self._saltar:
                self._saltar -= 1
            return
        if self._saltar:
            return

        if tag == "pre" and self._pre is not None:
            codigo = "".join(self._pre).strip()
            self._pre = None
            if codigo:
                if len(codigo) > MAX_CARACTERES_VISUAL:
                    codigo = codigo[:MAX_CARACTERES_VISUAL] + "\n...[recortado]"
                self.bloques.append(
                    "<<<ELEMENTO_VISUAL>>>\n" + codigo + "\n<<<FIN_ELEMENTO_VISUAL>>>"
                )
            return

        if tag == "math" and self._math is not None:
            formula = " ".join(
                "".join(self._annotation or self._math).split()
            ).strip()
            self._math = None
            self._annotation = None
            if formula:
                self._buffer.append(f" <<<FORMULA: {formula[:300]}>>> ")
            return

        if tag == "annotation":
            self._annotation_cerrada = True
            return

        if tag in ENCABEZADOS:
            texto = self._texto_buffer()
            if texto and self._titulo is None and tag in ("h1", "h2"):
                self._titulo = texto
            self._flush()
        elif tag in BLOQUES or tag == "tr":
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._saltar:
            return
        if self._pre is not None:
            self._pre.append(data)
            return
        if self._math is not None:
            self._math.append(data)
            if self._annotation is not None and not self._annotation_cerrada:
                self._annotation.append(data)
            return
        self._buffer.append(data)

    def cerrar(self) -> tuple[list[str], str | None]:
        self._flush()
        return self.bloques, self._titulo


def extraer_xhtml(ruta: Path) -> tuple[list[str], str | None]:
    parser = ExtractorTexto()
    parser.feed(ruta.read_text(encoding="utf-8", errors="replace"))
    parser.close()
    return parser.cerrar()


# ---------------------------------------------------------------------------
# Lectura del EPUB extraído
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def leer_paquete(libro: Path) -> tuple[Path, str | None, list[Path]]:
    """Devuelve el OPF, el título y los XHTML en orden de lectura."""
    container = libro / "META-INF" / "container.xml"
    if not container.is_file():
        raise SystemExit(f"No es un EPUB extraído: falta {container}")

    arbol_container = ElementTree.parse(container).getroot()
    ruta_opf = None
    for elemento in arbol_container.iter():
        if _local(elemento.tag) == "rootfile" and elemento.get("full-path"):
            ruta_opf = libro / unquote(elemento.get("full-path", ""))
            break
    if ruta_opf is None or not ruta_opf.is_file():
        raise SystemExit("No encuentro el archivo .opf en META-INF/container.xml")

    arbol = ElementTree.parse(ruta_opf).getroot()

    titulo = None
    for elemento in arbol.iter():
        if _local(elemento.tag) == "title" and (elemento.text or "").strip():
            titulo = elemento.text.strip()
            break

    manifiesto: dict[str, dict[str, str]] = {}
    for elemento in arbol.iter():
        if _local(elemento.tag) != "item":
            continue
        identificador = elemento.get("id")
        href = elemento.get("href")
        if not identificador or not href:
            continue
        manifiesto[identificador] = {
            "href": href,
            "media": elemento.get("media-type", ""),
            "props": elemento.get("properties", ""),
        }

    capitulos: list[Path] = []
    for elemento in arbol.iter():
        if _local(elemento.tag) != "itemref":
            continue
        if elemento.get("linear", "yes") == "no":
            continue

        item = manifiesto.get(elemento.get("idref", ""))
        if not item:
            continue

        href = item["href"]
        if "html" not in item["media"] and not href.lower().endswith(
            (".xhtml", ".html", ".htm")
        ):
            continue
        if "nav" in item["props"].split():
            continue

        ruta = (ruta_opf.parent / unquote(href)).resolve()
        if not ruta.is_file():
            print(f"  aviso: falta {ruta.relative_to(libro)}; se omite")
            continue
        if ruta not in capitulos:
            capitulos.append(ruta)

    return ruta_opf, titulo, capitulos


def _candidatos_epub() -> list[Path]:
    if not HTML_DIR.is_dir():
        return []
    return sorted(
        ruta
        for ruta in HTML_DIR.iterdir()
        if ruta.is_dir() and (ruta / "META-INF" / "container.xml").is_file()
    )


def _candidatos_pdf() -> list[Path]:
    if not PDF_DIR.is_dir():
        return []
    return sorted(
        ruta
        for ruta in PDF_DIR.iterdir()
        if ruta.is_dir() and (ruta / "texto.txt").is_file()
    )


def _seleccionar(candidatos: list[Path], nombre: str | None) -> Path | None:
    """Carpeta que coincide con el nombre, o None si no hay ninguna."""
    if not candidatos:
        return None
    if nombre is None:
        return candidatos[0] if len(candidatos) == 1 else None

    exactos = [ruta for ruta in candidatos if ruta.name == nombre]
    if exactos:
        return exactos[0]

    parciales = [ruta for ruta in candidatos if nombre.lower() in ruta.name.lower()]
    if len(parciales) == 1:
        return parciales[0]
    if len(parciales) > 1:
        listado = "\n".join(f"  - {ruta.name}" for ruta in parciales)
        raise SystemExit(f"El nombre coincide con varios libros:\n{listado}")
    return None


def elegir_libro(nombre: str | None) -> Path:
    candidatos = _candidatos_epub()

    if not candidatos:
        raise SystemExit(f"No hay EPUB extraídos en {HTML_DIR}. Ejecuta extract_epub.py.")

    ruta = _seleccionar(candidatos, nombre)
    if ruta is not None:
        return ruta

    if nombre:
        raise SystemExit(f"No encuentro el libro: {nombre}")

    listado = "\n".join(f"  - {ruta.name}" for ruta in candidatos)
    raise SystemExit(f"Hay varios libros. Usa --libro:\n{listado}")


def elegir_fuente(nombre: str | None, preferir: str | None = None) -> tuple[str, Path]:
    """Devuelve («epub» o «pdf», carpeta). EPUB tiene prioridad si no se fija."""
    fuentes: list[tuple[str, list[Path]]] = []
    if preferir != "pdf":
        fuentes.append(("epub", _candidatos_epub()))
    if preferir != "epub":
        fuentes.append(("pdf", _candidatos_pdf()))

    for tipo, candidatos in fuentes:
        ruta = _seleccionar(candidatos, nombre)
        if ruta is not None:
            return tipo, ruta

    if nombre:
        raise SystemExit(
            f"No encuentro el libro: {nombre}\n"
            f"  EPUB extraídos en {HTML_DIR}\n  PDF extraídos en {PDF_DIR}"
        )

    listado = "\n".join(
        f"  - {ruta.name}  ({tipo})" for tipo, candidatos in fuentes for ruta in candidatos
    )
    if not listado:
        raise SystemExit(
            f"No hay libros preparados. Ejecuta extract_epub.py o extract_pdf.py."
        )
    raise SystemExit(f"Hay varios libros. Usa --libro:\n{listado}")


# ---------------------------------------------------------------------------
# Texto plano extraído de un PDF
# ---------------------------------------------------------------------------


def leer_texto_plano(carpeta: Path) -> tuple[str | None, list[tuple[str, list[str]]]]:
    """Lee texto.txt de un PDF extraído como unidades de lectura por página."""
    manifiesto_ruta = carpeta / "manifiesto.json"
    titulo = carpeta.name
    if manifiesto_ruta.is_file():
        try:
            datos = json.loads(manifiesto_ruta.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            datos = {}
        titulo = datos.get("titulo") or datos.get("libro") or carpeta.name

    texto = (carpeta / "texto.txt").read_text(encoding="utf-8")
    unidades: list[tuple[str, list[str]]] = []
    for numero, pagina in enumerate(texto.split("\f"), start=1):
        parrafos = [parrafo.strip() for parrafo in re.split(r"\n\s*\n", pagina)]
        parrafos = [parrafo for parrafo in parrafos if parrafo]
        if parrafos:
            unidades.append((f"página {numero}", parrafos))

    return titulo, unidades


# ---------------------------------------------------------------------------
# Llamadas al LLM
# ---------------------------------------------------------------------------


def cabeceras() -> dict[str, str]:
    return {"Authorization": f"Bearer {KEY}"}


def liberar_otros_modelos() -> None:
    """Descarga los modelos que no sean Gemma para liberar VRAM."""
    respuesta = requests.get(f"{BASE}/v1/models", headers=cabeceras(), timeout=60)
    respuesta.raise_for_status()

    for modelo in respuesta.json().get("data", []):
        if not modelo.get("loaded") or modelo.get("id") == MODELO:
            continue
        print(f"Liberando VRAM: {modelo['id']}")
        liberacion = requests.post(
            f"{BASE}/api/inference/unload",
            json={"model_path": modelo["id"], "force_cancel_active": True},
            headers=cabeceras(),
            timeout=300,
        )
        if liberacion.status_code != 200:
            print(
                f"  aviso: no se pudo liberar {modelo['id']} "
                f"(HTTP {liberacion.status_code})"
            )


def cargar_modelo(permitir_carga: bool = True) -> None:
    respuesta = requests.get(f"{BASE}/v1/models", headers=cabeceras(), timeout=60)
    respuesta.raise_for_status()

    for modelo in respuesta.json().get("data", []):
        if modelo.get("id") == MODELO:
            if modelo.get("loaded"):
                return
            break

    if not permitir_carga:
        raise SystemExit(
            f"{MODELO} no está cargado. Cárgalo en Unsloth Studio o ejecuta sin --sin-cargar."
        )

    print(f"Cargando {MODELO}...")
    carga = {
        "model_path": MODELO,
        "max_seq_length": MAX_SEQ_LENGTH,
        "cache_type_kv": KV_CACHE,
    }
    respuesta = requests.post(
        f"{BASE}/api/inference/load",
        json=carga,
        headers=cabeceras(),
        timeout=1800,
    )

    # El TTS puede seguir ocupando la VRAM que necesita Gemma.
    if respuesta.status_code == 400 and "out_of_memory" in respuesta.text:
        liberar_otros_modelos()
        respuesta = requests.post(
            f"{BASE}/api/inference/load",
            json=carga,
            headers=cabeceras(),
            timeout=1800,
        )

    if respuesta.status_code != 200:
        detalle = respuesta.text[:600]
        raise SystemExit(
            f"No se pudo cargar {MODELO} (HTTP {respuesta.status_code}): {detalle}\n"
            "Libera VRAM o carga el modelo manualmente desde Unsloth Studio."
        )


def contar_palabras(texto: str) -> int:
    return len(texto.split())


def palabras_utiles(texto: str) -> int:
    """Palabras que el LLM debe conservar; el código visual no cuenta."""
    sin_visual = re.sub(
        r"<<<ELEMENTO_VISUAL>>>.*?<<<FIN_ELEMENTO_VISUAL>>>", " ", texto, flags=re.S
    )
    sin_marcas = re.sub(r"<<<[^>]+>>>", " ", sin_visual)
    return contar_palabras(sin_marcas)


# ---------------------------------------------------------------------------
# Expansión determinista de monedas y porcentajes
# ---------------------------------------------------------------------------

_UNIDADES = [
    "cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho",
    "nueve", "diez", "once", "doce", "trece", "catorce", "quince", "dieciséis",
    "diecisiete", "dieciocho", "diecinueve", "veinte", "veintiuno", "veintidós",
    "veintitrés", "veinticuatro", "veinticinco", "veintiséis", "veintisiete",
    "veintiocho", "veintinueve",
]
_DECENAS = {
    30: "treinta", 40: "cuarenta", 50: "cincuenta", 60: "sesenta",
    70: "setenta", 80: "ochenta", 90: "noventa",
}
_CENTENAS = {
    100: "ciento",
    200: "doscientos", 300: "trescientos", 400: "cuatrocientos",
    500: "quinientos", 600: "seiscientos", 700: "setecientos",
    800: "ochocientos", 900: "novecientos",
}

# M = millones, MM y B = mil millones, K = miles. Es la convención del texto.
_MONEDA = re.compile(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)(?:\s*(MM|M|B|K))?")
_PORCENTAJE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*%")
_FACTORES = {"K": 1_000, "M": 1_000_000, "MM": 1_000_000_000, "B": 1_000_000_000}


def _hasta_99(n: int) -> str:
    if n < 30:
        return _UNIDADES[n]
    decena, unidad = divmod(n, 10)
    texto = _DECENAS[decena * 10]
    if unidad:
        texto += " y " + _UNIDADES[unidad]
    return texto


def _hasta_999(n: int) -> str:
    if n < 100:
        return _hasta_99(n)
    if n == 100:
        return "cien"
    centena, resto = divmod(n, 100)
    texto = _CENTENAS[centena * 100]
    if resto:
        texto += " " + _hasta_99(resto)
    return texto


def _apocope(texto: str) -> str:
    if texto.endswith("veintiuno"):
        return texto[: -len("veintiuno")] + "veintiún"
    if texto.endswith("uno"):
        return texto[:-3] + "un"
    return texto


def numero_a_letras(n: int) -> str:
    if n < 0:
        return "menos " + numero_a_letras(-n)
    if n < 1_000:
        return _hasta_999(n)

    if n < 1_000_000:
        miles, resto = divmod(n, 1_000)
        texto = "mil" if miles == 1 else _apocope(_hasta_999(miles)) + " mil"
        if resto:
            texto += " " + _hasta_999(resto)
        return texto

    if n < 1_000_000_000_000:
        millones, resto = divmod(n, 1_000_000)
        texto = (
            "un millón"
            if millones == 1
            else _apocope(numero_a_letras(millones)) + " millones"
        )
        if resto:
            texto += " " + numero_a_letras(resto)
        return texto

    billones, resto = divmod(n, 1_000_000_000_000)
    texto = (
        "un billón"
        if billones == 1
        else _apocope(numero_a_letras(billones)) + " billones"
    )
    if resto:
        texto += " " + numero_a_letras(resto)
    return texto


def _importe(valor: float) -> str:
    entero = int(valor)
    if valor == entero:
        palabras = numero_a_letras(entero)
        if entero == 1:
            return "un dólar"
        if entero >= 1_000_000:
            return palabras + " de dólares"
        return palabras + " dólares"

    # Importes con centavos, por ejemplo $1.50.
    entero = int(valor)
    centavos = round((valor - entero) * 100)
    if centavos == 100:
        entero += 1
        centavos = 0
    texto = "un dólar" if entero == 1 else numero_a_letras(entero) + " dólares"
    if centavos:
        texto += " con " + numero_a_letras(centavos) + " centavos"
    return texto


def _reemplazar_moneda(coincidencia: re.Match[str]) -> str:
    bruto = coincidencia.group(1)
    sufijo = coincidencia.group(2) or ""

    if "." in bruto:
        entero_txt, decimal_txt = bruto.split(".", 1)
    else:
        entero_txt, decimal_txt = bruto, ""

    entero = int(entero_txt.replace(",", "") or "0")
    factor = _FACTORES.get(sufijo, 1)

    if factor != 1:
        valor = entero * factor
        if decimal_txt:
            valor += round(int(decimal_txt) / (10 ** len(decimal_txt)) * factor)
        return _importe(float(valor))

    valor = entero + (int(decimal_txt) / (10 ** len(decimal_txt)) if decimal_txt else 0)
    return _importe(valor)


def _reemplazar_porcentaje(coincidencia: re.Match[str]) -> str:
    bruto = coincidencia.group(1).replace(",", ".")
    if "." not in bruto:
        return numero_a_letras(int(bruto)) + " por ciento"

    entero_txt, decimal_txt = bruto.split(".", 1)
    entero = numero_a_letras(int(entero_txt))
    decimales = " ".join(_UNIDADES[int(digito)] for digito in decimal_txt)
    return f"{entero} coma {decimales} por ciento"


def expandir_numeros(texto: str) -> str:
    """Expande monedas y porcentajes fuera de los bloques visuales."""
    partes = re.split(
        r"(<<<ELEMENTO_VISUAL>>>.*?<<<FIN_ELEMENTO_VISUAL>>>)", texto, flags=re.S
    )
    for indice, parte in enumerate(partes):
        if parte.startswith("<<<ELEMENTO_VISUAL>>>"):
            continue
        parte = _MONEDA.sub(_reemplazar_moneda, parte)
        partes[indice] = _PORCENTAJE.sub(_reemplazar_porcentaje, parte)
    return "".join(partes)


def limpiar_salida(texto: str) -> str:
    texto = texto.replace("\r\n", "\n").replace("\r", "\n").strip()

    texto = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", texto)
    texto = re.sub(r"\n```$", "", texto)

    # El modelo a veces devuelve énfasis de Markdown que no debe leerse.
    texto = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", texto)
    texto = re.sub(r"(?<!\w)\*([^*\n]+)\*(?!\w)", r"\1", texto)
    texto = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", texto)
    texto = texto.replace("`", "")

    texto = re.sub(
        r"^(aquí (está|tienes)|claro|por supuesto|con gusto)[^\n]{0,100}:\s*\n+",
        "",
        texto,
        flags=re.IGNORECASE,
    )
    texto = re.sub(r"[ \t]+\n", "\n", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


_CORREO = re.compile(r"<?\S+@\S+\.\S+>?")
_URL = re.compile(r"https?://\S+")
_DOMINIO = re.compile(
    r"\b([a-z0-9-]+)\.(com|org|net|mx|edu|gob|gov|io)\b", flags=re.IGNORECASE
)
_ENTERO = re.compile(r"(?<![\w.,-])(\d{1,4})(?![\w.,-])")
_ENUMERADOR = re.compile(r"^\d{1,2}[.)]\s+")
# Enumeración dentro de la línea: «…productividad sistémica. 2. Explotar…».
# Solo detrás de un punto o dos puntos, para no tocar «el 26. La cifra».
_ENUMERADOR_INLINE = re.compile(
    r"(?<=[.:;])\s+(\d{1,2})[.)]\s+(?=[A-ZÁÉÍÓÚÑ][a-záéíóúñ])"
)
_FIN_DE_FRASE = re.compile(r"[.!?…:;]$")
_CONTACTO = re.compile(r"^(contacto|correo|email|e-mail|teléfono|telefono)\b", re.I)
_PASO = re.compile(r"\b(Paso|Capítulo|Capitulo|Parte|Sección|Seccion|Bloque)\s+(\d{1,3})\b", re.I)
_CANTIDAD_ESTRUCTURA = re.compile(
    r"\b(\d{1,3})\s+(pasos?|capítulos?|capitulos?|partes?|secciones?|bloques?)\b",
    re.I,
)
_DEFINICION = re.compile(r"(?m)^([^()\n:]{2,60}?)\s*\(([A-Z]{1,3})\)\s*:")
_VARIABLES = re.compile(r"\(([A-Z]{1,3}(?:\s*,\s*[A-Z]{1,3})*)\)")


def _titulo_corto(linea: str) -> bool:
    """Línea que parece encabezado y aún puede contener cifras sin expandir."""
    return len(linea.split()) <= 12 and not _FIN_DE_FRASE.search(linea)


def _numeros_en_titulo(linea: str) -> str:
    linea = re.sub(
        r"^(\d{1,2})([.)])\s+",
        lambda m: numero_a_letras(int(m.group(1))) + m.group(2) + " ",
        linea,
    )
    linea = _ENTERO.sub(lambda m: numero_a_letras(int(m.group(1))), linea)
    if linea and linea[0].islower():
        linea = linea[0].upper() + linea[1:]
    return linea


def _es_contacto(linea: str) -> bool:
    if not _CONTACTO.match(linea):
        return False
    minuscula = linea.lower()
    return " arroba " in minuscula or "punto com" in minuscula or "punto mx" in minuscula


def _normalizar_variables(texto: str) -> str:
    """Quita letras redundantes y expande listas como «(T, I, OE)»."""
    nombres: dict[str, str] = {}
    for coincidencia in _DEFINICION.finditer(texto):
        nombre = coincidencia.group(1).strip()
        letra = coincidencia.group(2)
        if 3 <= len(nombre):
            nombres.setdefault(letra, nombre)

    # «Throughput (T):» → «Throughput:»
    texto = _DEFINICION.sub(lambda m: f"{m.group(1).strip()}:", texto)

    def expandir(coincidencia: re.Match[str]) -> str:
        letras = re.findall(r"[A-Z]{1,3}", coincidencia.group(1))
        if len(letras) < 2 or not all(letra in nombres for letra in letras):
            return coincidencia.group(0)
        partes = [nombres[letra].lower() for letra in letras]
        return "(" + ", ".join(partes[:-1]) + " y " + partes[-1] + ")"

    texto = _VARIABLES.sub(expandir, texto)

    # Siglas de variable de dos o más letras sueltas en la prosa: «el OE».
    for letra, nombre in nombres.items():
        if len(letra) >= 2:
            texto = re.sub(rf"\b{re.escape(letra)}\b", nombre.lower(), texto)
    return texto


def normalizar_salida(texto: str) -> str:
    """Quita contactos, verbaliza dominios y numera los encabezados con palabras."""
    texto = re.sub(r"\bEE\.\s*UU\.", "Estados Unidos", texto)
    lineas: list[str] = []
    for linea in texto.splitlines():
        recortada = linea.strip()
        if not recortada:
            lineas.append("")
            continue
        if (
            _CORREO.fullmatch(recortada)
            or _URL.fullmatch(recortada)
            or _es_contacto(recortada)
        ):
            continue

        recortada = _DOMINIO.sub(lambda m: f"{m.group(1)} punto {m.group(2)}", recortada)
        # Enumeraciones que quedaron dentro de la línea tras la preparación.
        recortada = _ENUMERADOR_INLINE.sub(
            lambda m: " " + numero_a_letras(int(m.group(1))).capitalize() + ".",
            recortada,
        )
        recortada = _PASO.sub(
            lambda m: f"{m.group(1)} {numero_a_letras(int(m.group(2)))}", recortada
        )
        recortada = _CANTIDAD_ESTRUCTURA.sub(
            lambda m: f"{numero_a_letras(int(m.group(1)))} {m.group(2)}", recortada
        )
        if _ENUMERADOR.match(recortada) or _titulo_corto(recortada):
            recortada = _numeros_en_titulo(recortada)
        lineas.append(recortada)

    texto = re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()
    return _normalizar_variables(texto)


def llamar_llm(texto: str, reforzar: bool = False) -> str:
    sistema = SISTEMA + (REFUERZO if reforzar else "")
    payload = {
        "model": MODELO,
        "messages": [
            {"role": "system", "content": sistema},
            {"role": "user", "content": texto},
        ],
        "temperature": TEMPERATURA,
        "max_tokens": MAX_TOKENS,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    for intento in range(REINTENTOS):
        try:
            respuesta = requests.post(
                f"{BASE}/v1/chat/completions",
                json=payload,
                headers=cabeceras(),
                timeout=1800,
            )
        except requests.RequestException as error:
            if intento == REINTENTOS - 1:
                raise SystemExit(f"Error de conexión con el LLM: {error}")
            time.sleep(2**intento)
            continue

        if respuesta.status_code == 200:
            mensaje = respuesta.json()["choices"][0]["message"]
            return limpiar_salida(mensaje.get("content") or "")

        if respuesta.status_code >= 500 and intento < REINTENTOS - 1:
            time.sleep(2**intento)
            continue
        raise SystemExit(
            f"El LLM devolvió HTTP {respuesta.status_code}: {respuesta.text[:500]}"
        )

    return ""


def _extensión_imagen(ruta: Path) -> str:
    return TIPOS_IMAGEN.get(ruta.suffix.lower().lstrip("."), "image/jpeg")


def describir_imagen(ruta: Path) -> str:
    """Describe una figura con la parte de visión de Gemma y cachea el resultado."""
    if not ruta.is_file():
        print(f"  aviso: falta la imagen {ruta}")
        return ""

    datos = ruta.read_bytes()
    digest = hashlib.sha256(
        f"{VERSION_IMAGEN}\0{MODELO}\0".encode("utf-8") + datos
    ).hexdigest()
    cache = CACHE_IMAGENES_DIR / f"{digest}.txt"
    if cache.is_file():
        return cache.read_text(encoding="utf-8").strip()

    payload = {
        "model": MODELO,
        "messages": [
            {"role": "system", "content": SISTEMA_IMAGEN},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Describe esta figura para el narrador del audiolibro.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:"
                            + _extensión_imagen(ruta)
                            + ";base64,"
                            + base64.b64encode(datos).decode("ascii")
                        },
                    },
                ],
            },
        ],
        "temperature": TEMPERATURA,
        "max_tokens": MAX_TOKENS_IMAGEN,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    mejor = ""
    for intento in range(REINTENTOS):
        try:
            respuesta = requests.post(
                f"{BASE}/v1/chat/completions",
                json=payload,
                headers=cabeceras(),
                timeout=900,
            )
        except requests.RequestException as error:
            print(f"  aviso: sin respuesta del LLM de visión: {error}")
            break

        if respuesta.status_code == 200:
            crudo = limpiar_salida(respuesta.json()["choices"][0]["message"].get("content") or "")
            crudo = re.sub(r"[*_`]+", "", crudo)  # cursivas de Markdown
            crudo = re.sub(r"^[\"«”\s]+|[\"»“\s]+$", "", crudo)
            crudo = re.sub(r"\s+", " ", crudo).strip()
            if contar_palabras(crudo) > contar_palabras(mejor):
                mejor = crudo
            break

        if respuesta.status_code == 400 and "out_of_memory" in respuesta.text:
            liberar_otros_modelos()
            cargar_modelo()
            continue

        print(
            f"  aviso: visión HTTP {respuesta.status_code}: "
            f"{respuesta.text[:200]}"
        )
        break

    if mejor:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(mejor, encoding="utf-8")
    return mejor


def resolver_imagenes(bloque: str, base: Path) -> str:
    """Cambia `<<<IMAGEN: imagenes/x.jpg>>>` por su descripción en texto."""
    if "<<<IMAGEN:" not in bloque:
        return bloque

    def sustituir(coincidencia: re.Match[str]) -> str:
        ruta = (base / coincidencia.group(1)).resolve()
        if not ruta.is_relative_to(base.resolve()):
            return ""
        descripcion = describir_imagen(ruta)
        return f" <<<IMAGEN: {descripcion}>>> " if descripcion else ""

    return re.sub(r"<<<IMAGEN:\s*(imagenes/[^<>\s]+)\s*>>>", sustituir, bloque)


def fragmentar(texto: str, limite: int) -> list[str]:
    """Divide un párrafo demasiado largo por frases."""
    if palabras_utiles(texto) <= limite:
        return [texto]

    frases = re.split(r"(?<=[.!?…])\s+", texto)
    trozos: list[str] = []
    actual: list[str] = []
    cuenta = 0

    for frase in frases:
        peso = contar_palabras(frase)
        if actual and cuenta + peso > limite:
            trozos.append(" ".join(actual))
            actual = []
            cuenta = 0
        actual.append(frase)
        cuenta += peso

    if actual:
        trozos.append(" ".join(actual))
    return trozos


def agrupar_llamadas(parrafos: list[str], limite: int) -> list[str]:
    """Agrupa párrafos en fragmentos que quepan en una llamada al LLM."""
    piezas: list[str] = []
    for parrafo in parrafos:
        piezas.extend(fragmentar(parrafo, limite))

    grupos: list[str] = []
    actual: list[str] = []
    cuenta = 0

    for pieza in piezas:
        peso = palabras_utiles(pieza)
        if actual and cuenta + peso > limite:
            grupos.append("\n\n".join(actual))
            actual = []
            cuenta = 0
        actual.append(pieza)
        cuenta += peso

    if actual:
        grupos.append("\n\n".join(actual))
    return grupos


def preparar_fragmento(texto: str, capitulo: str) -> str:
    texto = expandir_numeros(texto)
    util = palabras_utiles(texto)
    if util < 5:
        # Fragmento compuesto casi solo por un diagrama: no hay prosa que leer.
        if "<<<ELEMENTO_VISUAL>>>" in texto:
            return ""
        return normalizar_salida(limpiar_salida(texto))

    digest = hashlib.sha256(
        f"{VERSION_PROMPT}\0{MODELO}\0{texto}".encode("utf-8")
    ).hexdigest()
    cache = SALIDA_DIR / "cache" / f"{digest}.txt"

    # La caché guarda la salida cruda del modelo. La normalización se aplica al
    # leer, así los cambios de normalización no obligan a regenerar el texto.
    if cache.is_file():
        return normalizar_salida(cache.read_text(encoding="utf-8"))

    mejor = ""
    mejor_crudo = ""
    for intento in range(REINTENTOS):
        crudo = llamar_llm(texto, reforzar=intento > 0)
        if not crudo:
            continue
        if "<<<ELEMENTO_VISUAL>>>" in crudo or "<<<FIN_ELEMENTO_VISUAL>>>" in crudo:
            crudo = re.sub(r"<<<[^>]+>>>", "", crudo, flags=re.S).strip()
        salida = normalizar_salida(crudo)
        if contar_palabras(salida) > contar_palabras(mejor):
            mejor = salida
            mejor_crudo = crudo

        if contar_palabras(salida) >= 0.55 * util:
            break

        print(
            f"  aviso: salida corta ({contar_palabras(salida)}/{util} palabras); "
            "reintentando"
        )

    if not mejor:
        raise SystemExit(f"No se pudo preparar el fragmento del capítulo: {capitulo}")

    if contar_palabras(mejor) < 0.55 * util:
        print(
            f"  aviso: el fragmento quedó más corto de lo esperado "
            f"({contar_palabras(mejor)}/{util} palabras)"
        )

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(mejor_crudo, encoding="utf-8")
    return mejor


# ---------------------------------------------------------------------------
# Ensamblado en bloques de audio
# ---------------------------------------------------------------------------


def bloques_de_audio(
    parrafos: list[Parrafo],
    wpm: int,
    minutos_objetivo: int,
    minutos_min: int = MINUTOS_MIN,
    minutos_max: int = MINUTOS_MAX,
) -> tuple[list[list[Parrafo]], int, int, int]:
    objetivo = int(wpm * minutos_objetivo)
    minimo = int(wpm * minutos_min)
    maximo = int(wpm * minutos_max)

    piezas: list[Parrafo] = []
    for parrafo in parrafos:
        for trozo in fragmentar(parrafo.texto, maximo):
            piezas.append(Parrafo(trozo, parrafo.capitulo))

    if not piezas:
        return [], objetivo, minimo, maximo

    pesos = [contar_palabras(pieza.texto) for pieza in piezas]
    total = sum(pesos)

    # Número de bloques: el más cercano al objetivo. Si eso deja bloques por
    # encima del máximo, se añaden siempre que no queden por debajo del mínimo
    # (con 5 % de tolerancia para no crear bloques de tres minutos).
    minimo_tolerado = int(minimo * 0.95)
    cantidad = max(1, round(total / objetivo))
    cantidad = min(cantidad, len(piezas))
    if total / cantidad > maximo:
        candidato = math.ceil(total / maximo)
        if total / candidato >= minimo_tolerado:
            cantidad = candidato
    while cantidad > 1 and total / (cantidad - 1) <= maximo:
        cantidad -= 1

    # Cortes cerca de i*total/cantidad, prefiriendo un cambio de capítulo.
    acumulado = []
    suma = 0
    for peso in pesos:
        suma += peso
        acumulado.append(suma)

    cortes: list[int] = []
    inicio = 0
    for i in range(1, cantidad):
        ideal = total * i / cantidad
        base = bisect.bisect_left(acumulado, ideal)
        base = min(max(base + 1, inicio + 1), len(piezas) - (cantidad - i))

        margen = int(total / cantidad * 0.10)
        desde = max(inicio + 1, bisect.bisect_left(acumulado, ideal - margen) + 1)
        hasta = min(len(piezas) - (cantidad - i), bisect.bisect_right(acumulado, ideal + margen) + 1)

        mejor = base
        mejor_distancia = abs(acumulado[base - 1] - ideal)
        mejor_capitulo = None
        mejor_capitulo_distancia = None
        for corte in range(desde, hasta):
            if piezas[corte].capitulo == piezas[corte - 1].capitulo:
                continue
            distancia = abs(acumulado[corte - 1] - ideal)
            if mejor_capitulo_distancia is None or distancia < mejor_capitulo_distancia:
                mejor_capitulo = corte
                mejor_capitulo_distancia = distancia

        # Un corte en inicio de capítulo vale aunque quede unos segundos antes
        # o después del punto ideal: evita encabezados huérfanos.
        margen_capitulo = int(total / cantidad * 0.05)
        if (
            mejor_capitulo is not None
            and mejor_capitulo_distancia is not None
            and mejor_capitulo_distancia <= mejor_distancia + margen_capitulo
        ):
            mejor = mejor_capitulo

        cortes.append(mejor)
        inicio = mejor

    cortes.append(len(piezas))

    bloques: list[list[Parrafo]] = []
    inicio = 0
    for corte in cortes:
        bloque = piezas[inicio:corte]
        if bloque:
            bloques.append(bloque)
        inicio = corte

    return bloques, objetivo, minimo, maximo


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Prepara un EPUB o un PDF extraído para narración y lo divide en bloques."
        )
    )
    parser.add_argument(
        "--libro",
        help="Carpeta dentro de html/ o de ../pdf_extract/texto/ (si hay varias).",
    )
    parser.add_argument(
        "--fuente",
        choices=("auto", "epub", "pdf"),
        default="auto",
        help="Forzar el tipo de origen. «auto» busca EPUB y luego PDF.",
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=0,
        help="Procesa solo los primeros N capítulos o páginas (para pruebas).",
    )
    parser.add_argument(
        "--minutos",
        type=int,
        default=MINUTOS_OBJETIVO,
        help=f"Minutos objetivo por bloque (por defecto {MINUTOS_OBJETIVO}).",
    )
    parser.add_argument(
        "--wpm",
        type=int,
        default=PALABRAS_POR_MINUTO,
        help=f"Palabras por minuto del TTS (por defecto {PALABRAS_POR_MINUTO}).",
    )
    parser.add_argument(
        "--sin-cargar",
        action="store_true",
        help="No intenta cargar el modelo; falla si no está listo.",
    )
    parser.add_argument(
        "--sin-vision",
        action="store_true",
        help="En PDF no describe las figuras: se quitan las marcas de imagen.",
    )
    args = parser.parse_args()

    preferir = None if args.fuente == "auto" else args.fuente
    tipo, libro = elegir_fuente(args.libro, preferir)

    ruta_opf: Path | None = None
    unidades: list[tuple[str, list[str]]] = []
    if tipo == "epub":
        ruta_opf, titulo, capitulos = leer_paquete(libro)
        if args.limite:
            capitulos = capitulos[: args.limite]
        if not capitulos:
            raise SystemExit("El EPUB no tiene documentos XHTML en el spine.")
        for ruta in capitulos:
            bloques, encabezado = extraer_xhtml(ruta)
            capitulo = normalizar_salida(encabezado) if encabezado else ruta.stem
            unidades.append((capitulo, bloques))
    else:
        titulo, unidades = leer_texto_plano(libro)
        if args.limite:
            unidades = unidades[: args.limite]
        if not unidades:
            raise SystemExit(f"{libro / 'texto.txt'} no tiene texto.")

    print(f"Libro: {libro.name}")
    print(f"Origen: {'EPUB' if tipo == 'epub' else 'PDF (texto plano)'}")
    print(f"Título: {titulo or '(sin título)'}")
    print(f"Unidades: {len(unidades)}")

    if args.sin_cargar:
        cargar_modelo(permitir_carga=False)
    else:
        cargar_modelo()

    destino = SALIDA_DIR / libro.name
    destino.mkdir(parents=True, exist_ok=True)

    parrafos: list[Parrafo] = []
    origen_palabras = 0
    inicio = time.perf_counter()

    for indice, (capitulo, bloques) in enumerate(unidades, start=1):
        if tipo == "pdf":
            if args.sin_vision:
                bloques = [re.sub(r"<<<IMAGEN:[^>]*>>>", " ", b).strip() for b in bloques]
            else:
                bloques = [resolver_imagenes(b, libro) for b in bloques]
            bloques = [b for b in bloques if b]

        util = sum(palabras_utiles(bloque) for bloque in bloques)
        origen_palabras += util
        grupos = agrupar_llamadas(bloques, PALABRAS_POR_LLAMADA)

        print(
            f"[{indice}/{len(unidades)}] {capitulo} · {util} palabras · "
            f"{len(grupos)} fragmento(s)"
        )

        for numero, grupo in enumerate(grupos, start=1):
            t = time.perf_counter()
            preparado = preparar_fragmento(grupo, capitulo)
            dt = time.perf_counter() - t
            print(
                f"  fragmento {numero}/{len(grupos)} · "
                f"{contar_palabras(grupo)} → {contar_palabras(preparado)} palabras · "
                f"{dt:.1f}s"
            )
            for trozo in re.split(r"\n{2,}", preparado):
                trozo = trozo.strip()
                if trozo:
                    parrafos.append(Parrafo(trozo, capitulo))

    if not parrafos:
        raise SystemExit("No se extrajo texto preparado.")

    bloques, objetivo, minimo, maximo = bloques_de_audio(
        parrafos, args.wpm, args.minutos
    )

    manifiesto = {
        "libro": libro.name,
        "titulo": titulo,
        "origen": tipo,
        "opf": str(ruta_opf.relative_to(libro)) if ruta_opf else None,
        "modelo": MODELO,
        "palabras_por_minuto": args.wpm,
        "minutos_objetivo": args.minutos,
        "palabras_objetivo_por_bloque": objetivo,
        "palabras_minimas_por_bloque": minimo,
        "palabras_maximas_por_bloque": maximo,
        "palabras_origen": origen_palabras,
        "total_palabras": sum(contar_palabras(p.texto) for p in parrafos),
        "bloques": [],
    }

    texto_completo: list[str] = []
    for numero, bloque in enumerate(bloques, start=1):
        contenido = "\n\n".join(p.texto for p in bloque).strip() + "\n"
        nombre = f"bloque_{numero:02d}.txt"
        (destino / nombre).write_text(contenido, encoding="utf-8")
        texto_completo.append(contenido.strip())

        palabras = contar_palabras(contenido)
        minutos = palabras / args.wpm
        capitulos_bloque = list(dict.fromkeys(p.capitulo for p in bloque))
        manifiesto["bloques"].append(
            {
                "archivo": nombre,
                "palabras": palabras,
                "minutos_estimados": round(minutos, 2),
                "capitulos": capitulos_bloque,
            }
        )
        print(
            f"Bloque {numero:02d}: {palabras} palabras · {minutos:.1f} min · "
            f"{', '.join(capitulos_bloque)}"
        )
        if palabras < minimo or palabras > maximo:
            print(
                f"  aviso: fuera del rango de {minimo / args.wpm:.0f}–"
                f"{maximo / args.wpm:.0f} min; el total del libro no permite "
                "un reparto exacto."
            )

    (destino / "texto_completo.txt").write_text(
        "\n\n".join(texto_completo) + "\n", encoding="utf-8"
    )
    (destino / "manifiesto.json").write_text(
        json.dumps(manifiesto, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    total_palabras = manifiesto["total_palabras"]
    print(
        f"\nTotal: {total_palabras} palabras · "
        f"{total_palabras / args.wpm:.1f} min estimados · "
        f"{len(bloques)} bloque(s)"
    )
    print(f"Salida: {destino}")
    print(f"Tiempo: {time.perf_counter() - inicio:.1f}s")


if __name__ == "__main__":
    main()
