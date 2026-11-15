"""Extrae un PDF de src/ a texto plano en texto/<nombre>/.

No convierte el PDF a HTML: el texto que recibe el LLM es plano, sin etiquetas.
Las figuras se extraen como imágenes y se insertan en el texto con marcas
`<<<IMAGEN: imagenes/...>>>`; la etapa de preparación las describe con la
visión de Gemma-4-E4B (mmproj).

Uso:
    python extract_pdf.py                  # procesa todos los .pdf de src/
    python extract_pdf.py libro.pdf        # procesa solo ese archivo
    python extract_pdf.py libro --force    # reemplaza la extracción previa
    python extract_pdf.py libro --sin-ocr  # no intenta OCR
    python extract_pdf.py libro --sin-imagenes

Salida:
    texto/<nombre>/texto.txt
    texto/<nombre>/manifiesto.json
    texto/<nombre>/imagenes/              # solo si el PDF tiene figuras

`texto.txt` conserva las páginas separadas por un salto de página (\\f) para
que la preparación pueda decir de qué página viene cada párrafo. Las páginas
no llevan encabezados, pies ni números repetidos.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from xml.etree import ElementTree

RAIZ = Path(__file__).resolve().parent
ORIGEN = RAIZ / "src"
DESTINO = RAIZ / "texto"

# Una página con menos caracteres alfanuméricos que esto se considera escaneada.
UMBRAL_TEXTO_PAGINA = 40
# Idiomas de OCR, en orden de preferencia. Solo se usan los que tenga Tesseract.
IDIOMAS_OCR = ("spa", "eng")

# Imágenes: descartar iconos y páginas escaneadas completas.
MIN_ANCHO_IMAGEN = 80
MIN_ALTO_IMAGEN = 80
FRACCION_PAGINA_COMPLETA = 0.80
MAX_IMAGENES_POR_PAGINA = 8
MAX_LADO_IMAGEN = 1600
CALIDAD_JPEG = 90
# Resolución para renderizar figuras vectoriales (TikZ, diagramas, tablas).
DPI_FIGURA = 150

_RE_NUMERO_PAGINA = re.compile(
    r"^\s*(?:(?:p[áa]g(?:ina)?|page)\.?\s*)?[-–—]?\s*"
    r"(?:\d{1,4}|[ivxlcdm]{1,7})"
    r"\s*(?:(?:de|of|/)\s*\d{1,4})?\s*[-–—]?\s*$",
    re.IGNORECASE,
)
_RE_SOLO_SIMBOLOS = re.compile(r"^[\W_]+$", re.UNICODE)
_RE_ESPACIOS = re.compile(r"[ \t]+")
# Números de ecuación sueltos, como «(1)» o «(3b)», que no aportan a la lectura.
_RE_NUMERO_ECUACION = re.compile(r"^\(\d{1,3}[a-z]?\)$")
# Leyenda de figura o tabla: sirve para colocar la marca de imagen en su sitio.
_RE_LEYENDA = re.compile(
    r"^(figura|tabla|gr[áa]fico|cuadro|ilustraci[óo]n|imagen|mapa|diagrama)\s+\d+[.:]",
    re.IGNORECASE,
)
# Símbolos que delatan una expresión matemática volcada en bloques sueltos.
_RE_MATH = re.compile(r"[=+\-−*/^_<>≤≥≠≈±×÷∑∏∫√∞∂∇πθλμσωΔΩαβγδεζηκνρςφψ]")


@dataclass
class Linea:
    texto: str
    x: float
    y: float
    ancho: float
    alto: float


@dataclass
class Pagina:
    numero: int
    ancho: float
    alto: float
    bloques: list[list[Linea]] = field(default_factory=list)


@dataclass
class Imagen:
    pagina: int
    numero: int
    ancho: int
    alto: int
    xppi: float
    yppi: float
    archivo: str | None = None
    sha256: str | None = None
    omitida: str | None = None


# ---------------------------------------------------------------------------
# Utilidades del sistema
# ---------------------------------------------------------------------------


def ejecutar(comando: list[str], descripcion: str) -> subprocess.CompletedProcess:
    try:
        resultado = subprocess.run(comando, capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit(f"Falta la herramienta: {comando[0]}") from None

    if resultado.returncode != 0:
        detalle = (resultado.stderr or resultado.stdout).strip()[:800]
        raise SystemExit(f"Falló {descripcion}: {detalle}")
    return resultado


def disponible(nombre: str) -> bool:
    return shutil.which(nombre) is not None


def resolver(nombre: str) -> Path:
    """Acepta una ruta o un nombre dentro de src/, con o sin extensión."""
    ruta = Path(nombre).expanduser()
    if ruta.is_file():
        return ruta.resolve()

    for candidata in (
        ORIGEN / nombre,
        ORIGEN / f"{nombre}.pdf",
        ORIGEN / f"{nombre}.PDF",
    ):
        if candidata.is_file():
            return candidata

    raise SystemExit(f"No encuentro el PDF: {nombre}")


def pdfs_disponibles() -> list[Path]:
    return sorted(
        ruta
        for ruta in ORIGEN.iterdir()
        if ruta.is_file() and ruta.suffix.lower() == ".pdf"
    )


# ---------------------------------------------------------------------------
# Información del PDF
# ---------------------------------------------------------------------------


def _lineas_pdfinfo(pdf: Path) -> list[str]:
    resultado = ejecutar(
        ["pdfinfo", "-box", "-f", "1", "-l", "100000", str(pdf)],
        f"pdfinfo en {pdf.name}",
    )
    return resultado.stdout.splitlines()


def info_pdf(pdf: Path) -> dict:
    """Devuelve páginas, título y tamaño de cada página."""
    if not disponible("pdfinfo"):
        return {"paginas": 0, "titulo": None, "tamanos": {}}

    paginas = 0
    titulo: str | None = None
    tamanos: dict[int, tuple[float, float]] = {}

    for linea in _lineas_pdfinfo(pdf):
        if linea.startswith("Pages:"):
            paginas = int(linea.split(":", 1)[1].strip())
        elif linea.startswith("Title:"):
            # Los títulos fusionados de otros programas traen caracteres de control.
            valor = re.sub(r"[\x00-\x1f\x7f]", " ", linea.split(":", 1)[1])
            valor = re.sub(r"\s+", " ", valor).strip()
            titulo = valor or None
        else:
            coincidencia = re.match(
                r"^Page\s+(\d+)\s+size:\s+([\d.]+)\s+x\s+([\d.]+)", linea
            )
            if coincidencia:
                numero = int(coincidencia.group(1))
                tamanos[numero] = (
                    float(coincidencia.group(2)),
                    float(coincidencia.group(3)),
                )

    return {"paginas": paginas, "titulo": titulo, "tamanos": tamanos}


# ---------------------------------------------------------------------------
# Texto: extracción, OCR y estructura
# ---------------------------------------------------------------------------


def texto_plano(pdf: Path) -> tuple[str, str]:
    """Devuelve (texto, motor). Usa pdftotext y cae a mutool."""
    if disponible("pdftotext"):
        resultado = ejecutar(
            ["pdftotext", "-enc", "UTF-8", "-q", str(pdf), "-"],
            f"pdftotext en {pdf.name}",
        )
        return resultado.stdout, "pdftotext"

    if disponible("mutool"):
        resultado = ejecutar(
            ["mutool", "draw", "-q", "-F", "txt", "-o", "-", str(pdf)],
            f"mutool en {pdf.name}",
        )
        return resultado.stdout, "mutool"

    raise SystemExit("Falta pdftotext o mutool para extraer el texto.")


def paginas_de_texto(texto: str, total: int) -> list[str]:
    paginas = texto.replace("\r\n", "\n").replace("\r", "\n").split("\f")
    if paginas and not paginas[-1].strip():
        paginas.pop()
    if total and len(paginas) > total:
        paginas = paginas[:total]
    while len(paginas) < total:
        paginas.append("")
    return paginas


def _caracteres_utiles(texto: str) -> int:
    return sum(1 for caracter in texto if caracter.isalnum())


def paginas_sin_texto(paginas: list[str]) -> list[int]:
    return [
        indice
        for indice, texto in enumerate(paginas, start=1)
        if _caracteres_utiles(texto) < UMBRAL_TEXTO_PAGINA
    ]


def _rangos(numeros: list[int]) -> str:
    """Convierte [1,2,3,7,9,10] en '1-3,7,9-10'."""
    if not numeros:
        return ""
    numeros = sorted(set(numeros))
    partes: list[str] = []
    inicio = anterior = numeros[0]
    for numero in numeros[1:]:
        if numero == anterior + 1:
            anterior = numero
            continue
        partes.append(str(inicio) if inicio == anterior else f"{inicio}-{anterior}")
        inicio = anterior = numero
    partes.append(str(inicio) if inicio == anterior else f"{inicio}-{anterior}")
    return ",".join(partes)


def idiomas_ocr() -> str | None:
    if not disponible("tesseract"):
        return None
    try:
        resultado = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True
        )
    except OSError:
        return None

    disponibles = {
        linea.strip()
        for linea in resultado.stdout.splitlines()
        if linea.strip() and " " not in linea.strip()
    }
    elegidos = [idioma for idioma in IDIOMAS_OCR if idioma in disponibles]
    return "+".join(elegidos) if elegidos else None


def aplicar_ocr(pdf: Path, paginas: list[int], carpeta: Path) -> Path | None:
    """Hace OCR solo en las páginas indicadas. Devuelve el PDF resultante."""
    if not disponible("ocrmypdf"):
        print("  aviso: ocrmypdf no está instalado; sigo sin OCR")
        return None

    salida = carpeta / "ocr.pdf"
    comando = [
        "ocrmypdf",
        "--force-ocr",
        "--pages",
        _rangos(paginas),
        "--deskew",
        "--optimize",
        "0",
        "--output-type",
        "pdf",
        "--quiet",
    ]
    idioma = idiomas_ocr()
    if idioma:
        comando += ["--language", idioma]
    comando += [str(pdf), str(salida)]

    print(f"  OCR en {len(paginas)} página(s)" + (f" · {idioma}" if idioma else ""))
    try:
        resultado = subprocess.run(comando, capture_output=True, text=True)
    except OSError as error:
        print(f"  aviso: no se pudo ejecutar ocrmypdf: {error}")
        return None

    if resultado.returncode != 0:
        detalle = (resultado.stderr or resultado.stdout).strip()[:800]
        print(f"  aviso: falló el OCR; sigo con el texto original\n  {detalle}")
        return None

    return salida if salida.is_file() else None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _hijos(elemento: ElementTree.Element, nombre: str) -> list[ElementTree.Element]:
    return [hijo for hijo in elemento if _local(hijo.tag) == nombre]


def parsear_bbox(texto: str) -> list[Pagina]:
    """Convierte pdftotext -bbox-layout en páginas, bloques y líneas."""
    if not texto.strip():
        return []
    try:
        raiz = ElementTree.fromstring(texto)
    except ElementTree.ParseError:
        return []

    paginas: list[Pagina] = []
    for numero, elemento in enumerate(
        (nodo for nodo in raiz.iter() if _local(nodo.tag) == "page"), start=1
    ):
        ancho = float(elemento.get("width") or 0)
        alto = float(elemento.get("height") or 0)
        pagina = Pagina(numero=numero, ancho=ancho, alto=alto)

        flujos = _hijos(elemento, "flow") or [elemento]
        for flujo in flujos:
            for bloque in _hijos(flujo, "block"):
                lineas: list[Linea] = []
                for linea in _hijos(bloque, "line"):
                    palabras = [
                        (palabra.text or "").strip()
                        for palabra in _hijos(linea, "word")
                    ]
                    contenido = " ".join(p for p in palabras if p)
                    if not contenido:
                        continue
                    x = float(linea.get("xMin") or 0)
                    y = float(linea.get("yMin") or 0)
                    xmax = float(linea.get("xMax") or x)
                    ymax = float(linea.get("yMax") or y)
                    lineas.append(
                        Linea(
                            texto=contenido,
                            x=x,
                            y=y,
                            ancho=max(0.0, xmax - x),
                            alto=max(0.0, ymax - y),
                        )
                    )
                if lineas:
                    pagina.bloques.append(lineas)

        # Se conserva la página aunque no tenga bloques para que los números de
        # página coincidan con los del PDF.
        paginas.append(pagina)

    return paginas


def _bbox(pdf: Path) -> list[Pagina]:
    resultado = ejecutar(
        ["pdftotext", "-bbox-layout", "-enc", "UTF-8", "-q", str(pdf), "-"],
        f"pdftotext -bbox-layout en {pdf.name}",
    )
    return parsear_bbox(resultado.stdout)


# ---------------------------------------------------------------------------
# Limpieza: encabezados, pies, números de página y párrafos
# ---------------------------------------------------------------------------


def _firma(texto: str) -> str:
    texto = texto.lower()
    texto = re.sub(r"\d+", "#", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _zona(linea: Linea, pagina: Pagina) -> str | None:
    if pagina.alto <= 0:
        return None
    if linea.y < pagina.alto * 0.13:
        return "arriba"
    if linea.y > pagina.alto * 0.87:
        return "abajo"
    return None


def _es_numero_pagina(texto: str) -> bool:
    return bool(_RE_NUMERO_PAGINA.match(texto))


def _lineas_repetidas(paginas: list[Pagina]) -> set[tuple[str, str]]:
    """Detecta encabezados y pies que se repiten en varias páginas.

    Con dos páginas basta con que la línea aparezca en ambas; con más páginas
    se exige al menos la mitad. La zona es estrecha (13 % superior/inferior),
    así que un título de sección no entra ahí salvo que sea un encabezado.
    """
    if len(paginas) < 2:
        return set()

    conteo: Counter[tuple[str, str]] = Counter()
    for pagina in paginas:
        vistas: set[tuple[str, str]] = set()
        for bloque in pagina.bloques:
            for linea in bloque:
                zona = _zona(linea, pagina)
                if zona is None:
                    continue
                vistas.add((zona, _firma(linea.texto)))
        conteo.update(vistas)

    umbral = max(2, math.ceil(len(paginas) * 0.5))
    return {clave for clave, veces in conteo.items() if veces >= umbral}


def _filtrar_lineas(
    paginas: list[Pagina],
) -> list[Pagina]:
    repetidas = _lineas_repetidas(paginas)
    limpias: list[Pagina] = []

    for pagina in paginas:
        bloques: list[list[Linea]] = []
        for bloque in pagina.bloques:
            conservadas: list[Linea] = []
            for linea in bloque:
                zona = _zona(linea, pagina)
                texto = linea.texto.strip()
                if not texto:
                    continue
                if zona is not None and _es_numero_pagina(texto):
                    continue
                if zona is not None and (zona, _firma(texto)) in repetidas:
                    continue
                if _RE_SOLO_SIMBOLOS.match(texto):
                    continue
                conservadas.append(linea)
            if conservadas:
                bloques.append(conservadas)
        limpias.append(
            Pagina(
                numero=pagina.numero,
                ancho=pagina.ancho,
                alto=pagina.alto,
                bloques=bloques,
            )
        )

    return limpias


def _unir_lineas(lineas: list[Linea]) -> str:
    """Une líneas de un párrafo y corrige palabras cortadas por guion."""
    partes: list[str] = []
    for linea in lineas:
        texto = linea.texto.strip()
        if not texto:
            continue
        if partes:
            anterior = partes[-1]
            if anterior.endswith("\u00ad"):
                partes[-1] = anterior[:-1] + texto
                continue
            if anterior.endswith("-") and texto[:1].islower():
                partes[-1] = anterior[:-1] + texto
                continue
        partes.append(texto)
    return " ".join(partes)


def _parrafos_de_bloque(bloque: list[Linea]) -> list[str]:
    if not bloque:
        return []

    base_x = min(linea.x for linea in bloque)
    alturas = [linea.alto for linea in bloque if linea.alto > 0]
    altura = median(alturas) if alturas else 10.0
    saltos = [
        bloque[indice + 1].y - bloque[indice].y
        for indice in range(len(bloque) - 1)
        if bloque[indice + 1].y > bloque[indice].y
    ]
    salto_normal = median(saltos) if saltos else altura * 1.2

    umbral_sangria = max(3.0, altura * 0.8)
    umbral_salto = max(altura * 1.8, salto_normal * 1.5)

    parrafos: list[str] = []
    actual: list[Linea] = []
    anterior: Linea | None = None

    for linea in bloque:
        if anterior is not None:
            sangria = linea.x - base_x
            salto = linea.y - anterior.y
            if sangria > umbral_sangria or salto > umbral_salto:
                texto = _unir_lineas(actual)
                if texto:
                    parrafos.append(texto)
                actual = []
        actual.append(linea)
        anterior = linea

    texto = _unir_lineas(actual)
    if texto:
        parrafos.append(texto)
    return parrafos


def _limpiar_parrafo(texto: str) -> str:
    texto = unicodedata.normalize("NFKC", texto)
    for caracter in ("\u00ad", "\u200b", "\u200c", "\u200d", "\ufeff"):
        texto = texto.replace(caracter, "")
    texto = texto.replace("\u00a0", " ").replace("\u202f", " ")
    texto = _RE_ESPACIOS.sub(" ", texto).strip()
    return texto


def _es_formula(bloque: list[Linea]) -> bool:
    """Bloque corto con símbolos matemáticos: candidato a fórmula aislada."""
    texto = " ".join(linea.texto for linea in bloque)
    palabras = texto.split()
    if not palabras or len(palabras) > 12:
        return False
    if texto.rstrip().endswith("."):
        return False
    simbolos = _RE_MATH.findall(texto)
    # Un guion suelto en una línea corta no es una fórmula: hace falta un igual
    # o al menos dos símbolos matemáticos.
    return "=" in texto or len(simbolos) >= 2


def _es_fragmento(bloque: list[Linea]) -> bool:
    """Piezas sueltas de una fórmula: variables cortas en mayúsculas."""
    palabras = " ".join(linea.texto for linea in bloque).split()
    if not palabras or len(palabras) > 6:
        return False
    return all(len(palabra) <= 4 for palabra in palabras) and any(
        palabra.isalpha() and palabra.isupper() for palabra in palabras
    )


def _texto_formula(bloques: list[list[Linea]]) -> str:
    """Une las líneas de una fórmula: «T =» y la fracción «I / OE» por separado.

    `pdftotext` vuelca una ecuación centrada como líneas sueltas. La línea con
    el signo igual va primero; después se agrupan por pilas verticales las
    líneas que comparten columna horizontal (numerador sobre denominador), que
    se unen con «/».
    """
    lineas = [linea for bloque in bloques for linea in bloque if linea.texto.strip()]
    if not lineas:
        return ""

    principales = [linea for linea in lineas if "=" in linea.texto]
    principal = max(principales, key=lambda linea: (len(linea.texto.split()), -linea.y)) if principales else None
    restantes = [linea for linea in lineas if linea is not principal]

    def centro(linea: Linea) -> float:
        return linea.x + linea.ancho / 2

    pilas: list[list[Linea]] = []
    for linea in sorted(restantes, key=centro):
        if pilas:
            tope = pilas[-1][-1]
            solapado = abs(centro(linea) - centro(tope)) <= max(
                10.0, (tope.ancho + linea.ancho) / 2
            )
            cerca = abs(linea.y - (tope.y + tope.alto)) <= 24
            if solapado and cerca:
                pilas[-1].append(linea)
                continue
        pilas.append([linea])

    partes: list[str] = []
    if principal is not None:
        partes.append(principal.texto.strip())
    for pila in pilas:
        pila.sort(key=lambda linea: linea.y)
        unido = " / ".join(linea.texto.strip() for linea in pila)
        partes.append(unido)

    return _limpiar_parrafo(" ".join(partes))


def _parrafos_de_bloques(bloques: list[list[Linea]]) -> list[str]:
    """Genera párrafos y agrupa las fórmulas volcadas en bloques sueltos.

    Una ecuación centrada (`T = I / OE`) sale de `pdftotext` como varios bloques
    pequeños: `T =`, `I OE`, el número `(1)`. Aquí se reúnen las piezas cercanas
    y se marca el conjunto como fórmula para que el LLM la lea de una vez.
    """
    marcas = [(_es_formula(bloque), _es_fragmento(bloque)) for bloque in bloques]
    usados = [False] * len(bloques)

    def rango_y(indices: list[int]) -> tuple[float, float]:
        valores = [
            (min(linea.y for linea in bloques[i]),
             max(linea.y + linea.alto for linea in bloques[i]))
            for i in indices
        ]
        return min(v[0] for v in valores), max(v[1] for v in valores)

    def cerca(indices: list[int], candidato: int) -> bool:
        inicio, fin = rango_y(indices)
        y = min(linea.y for linea in bloques[candidato])
        alto = max(linea.y + linea.alto for linea in bloques[candidato])
        return y - fin <= 40 and inicio - alto <= 40

    grupos: dict[int, list[int]] = {}
    for indice, (es_formula, _) in enumerate(marcas):
        if not es_formula or usados[indice]:
            continue

        grupo = [indice]
        usados[indice] = True
        vecino = indice - 1
        while vecino >= 0 and marcas[vecino][1] and not usados[vecino] and cerca(grupo, vecino):
            grupo.append(vecino)
            usados[vecino] = True
            vecino -= 1
        vecino = indice + 1
        while vecino < len(bloques) and marcas[vecino][1] and not usados[vecino] and cerca(grupo, vecino):
            grupo.append(vecino)
            usados[vecino] = True
            vecino += 1

        grupo.sort()
        grupos[grupo[0]] = grupo

    parrafos: list[str] = []
    for indice, bloque in enumerate(bloques):
        if indice in grupos:
            miembros = grupos[indice]
            parrafos.append(
                f"<<<FORMULA: {_texto_formula([bloques[i] for i in miembros])}>>>"
            )
            continue
        if usados[indice]:
            continue
        for parrafo in _parrafos_de_bloque(bloque):
            limpio = _limpiar_parrafo(parrafo)
            if limpio:
                parrafos.append(limpio)

    return [
        parrafo
        for parrafo in parrafos
        if parrafo.startswith("<<<FORMULA:")
        or not (_es_numero_pagina(parrafo) or _RE_NUMERO_ECUACION.match(parrafo))
    ]


def _agrupar(valores: list[float], tolerancia: float) -> list[float]:
    """Agrupa coordenadas cercanas y devuelve el inicio de cada grupo."""
    grupos: list[list[float]] = []
    for valor in sorted(valores):
        if grupos and valor - grupos[-1][-1] <= tolerancia:
            grupos[-1].append(valor)
        else:
            grupos.append([valor])
    return [min(grupo) for grupo in grupos]


def _ordenar_bloques(pagina: Pagina) -> list[list[Linea]]:
    """Ordena los bloques en orden de lectura, columna por columna.

    `pdftotext -bbox-layout` entrega un flujo por columna, pero ordena por la
    posición vertical del bloque. En una página a dos columnas eso intercala el
    título de la columna derecha antes del cuerpo de la izquierda. Aquí se
    detectan las columnas por el borde izquierdo de los bloques y se leen de
    arriba abajo en cada una. Los bloques que ocupan el ancho completo (títulos
    generales, tablas anchas) separan bandas horizontales.
    """
    if len(pagina.bloques) < 2 or pagina.ancho <= 0:
        return pagina.bloques

    cajas: list[tuple[list[Linea], float, float, float, int]] = []
    for bloque in pagina.bloques:
        x = min(linea.x for linea in bloque)
        y = min(linea.y for linea in bloque)
        xmax = max(linea.x + linea.ancho for linea in bloque)
        palabras = sum(len(linea.texto.split()) for linea in bloque)
        cajas.append((bloque, x, y, xmax, palabras))

    umbral_ancho = pagina.ancho * 0.55
    estrechos = [caja for caja in cajas if caja[3] - caja[1] < umbral_ancho]
    anchos = [caja for caja in cajas if caja[3] - caja[1] >= umbral_ancho]

    grupos = _agrupar(
        [caja[1] for caja in estrechos], max(12.0, pagina.ancho * 0.03)
    )

    if len(grupos) >= 2:
        total = sum(caja[4] for caja in cajas) or 1
        peso = [0] * len(grupos)
        for caja in estrechos:
            indice = min(range(len(grupos)), key=lambda i: abs(caja[1] - grupos[i]))
            peso[indice] += caja[4]
        # Un grupo solo cuenta como columna si aporta texto suficiente; así un
        # título centrado no se confunde con una segunda columna.
        utiles = [
            indice
            for indice, palabras in enumerate(peso)
            if palabras >= total * 0.10
        ]
        if len(utiles) >= 2:
            centros = [grupos[indice] for indice in utiles]
            anchos.sort(key=lambda caja: caja[2])
            # Clave: (banda, 0 columnas / 1 ancho completo, columna, y, x).
            # Un bloque de ancho completo cierra su banda: va después de las
            # columnas que están por encima y antes de las que están por debajo.
            pares: list[tuple[tuple[int, int, int, float, float], list[Linea]]] = [
                ((indice, 1, -1, caja[2], caja[1]), caja[0])
                for indice, caja in enumerate(anchos)
            ]
            for caja in estrechos:
                columna = min(
                    range(len(centros)), key=lambda i: abs(caja[1] - centros[i])
                )
                banda = sum(1 for caja_ancha in anchos if caja_ancha[2] < caja[2])
                pares.append(((banda, 0, columna, caja[2], caja[1]), caja[0]))

            return [bloque for _, bloque in sorted(pares, key=lambda par: par[0])]

    return [caja[0] for caja in sorted(cajas, key=lambda caja: (caja[2], caja[1]))]


def parrafos_por_pagina(paginas: list[Pagina]) -> list[list[str]]:
    resultado: list[list[str]] = []
    for pagina in _filtrar_lineas(paginas):
        limpios = [
            _limpiar_parrafo(parrafo)
            for parrafo in _parrafos_de_bloques(_ordenar_bloques(pagina))
        ]
        resultado.append(
            [
                parrafo
                for parrafo in limpios
                if parrafo
                and not _es_numero_pagina(parrafo)
                and not _RE_NUMERO_ECUACION.match(parrafo)
            ]
        )
    return resultado


def parrafos_de_texto_plano(
    paginas: list[str],
) -> list[list[str]]:
    """Respaldo cuando pdftotext -bbox-layout no está disponible."""
    if len(paginas) < 2:
        repetidas: set[tuple[str, str]] = set()
    else:
        conteo: Counter[tuple[str, str]] = Counter()
        for texto in paginas:
            lineas = [linea.strip() for linea in texto.splitlines() if linea.strip()]
            vistas: set[tuple[str, str]] = set()
            if lineas:
                vistas.add(("arriba", _firma(lineas[0])))
                vistas.add(("abajo", _firma(lineas[-1])))
            if len(lineas) > 2:
                vistas.add(("arriba", _firma(lineas[1])))
                vistas.add(("abajo", _firma(lineas[-2])))
            conteo.update(vistas)
        umbral = max(2, math.ceil(len(paginas) * 0.5))
        repetidas = {clave for clave, veces in conteo.items() if veces >= umbral}

    resultado: list[list[str]] = []
    for texto in paginas:
        lineas = [linea.strip() for linea in texto.splitlines()]
        utiles = [linea for linea in lineas if linea]
        if utiles:
            if _es_numero_pagina(utiles[0]) or ("arriba", _firma(utiles[0])) in repetidas:
                utiles.pop(0)
            if utiles and (
                _es_numero_pagina(utiles[-1]) or ("abajo", _firma(utiles[-1])) in repetidas
            ):
                utiles.pop()

        parrafos: list[str] = []
        actual: list[str] = []
        for linea in utiles + [""]:
            if linea:
                actual.append(linea)
                continue
            if actual:
                parrafos.append(_unir_lineas(
                    [Linea(t, 0, 0, 0, 0) for t in actual]
                ))
                actual = []
        resultado.append(
            [
                limpio
                for limpio in (_limpiar_parrafo(p) for p in parrafos)
                if limpio
                and not _es_numero_pagina(limpio)
                and not _RE_NUMERO_ECUACION.match(limpio)
            ]
        )
    return resultado


# ---------------------------------------------------------------------------
# Figuras
# ---------------------------------------------------------------------------


def listar_imagenes(pdf: Path) -> list[Imagen]:
    if not disponible("pdfimages"):
        return []

    resultado = ejecutar(
        ["pdfimages", "-list", str(pdf)], f"pdfimages -list en {pdf.name}"
    )
    imagenes: list[Imagen] = []
    for linea in resultado.stdout.splitlines():
        campos = re.split(r"\s+", linea.strip())
        if len(campos) < 14 or not campos[0].isdigit():
            continue
        if campos[2] != "image":
            continue
        try:
            imagenes.append(
                Imagen(
                    pagina=int(campos[0]),
                    numero=int(campos[1]),
                    ancho=int(campos[3]),
                    alto=int(campos[4]),
                    xppi=float(campos[12]),
                    yppi=float(campos[13]),
                )
            )
        except ValueError:
            continue
    return imagenes


def _candidatas(
    imagenes: list[Imagen],
    tamanos: dict[int, tuple[float, float]],
) -> dict[int, list[Imagen]]:
    por_pagina: dict[int, list[Imagen]] = {}
    for imagen in imagenes:
        if imagen.ancho < MIN_ANCHO_IMAGEN or imagen.alto < MIN_ALTO_IMAGEN:
            continue
        if imagen.xppi <= 0 or imagen.yppi <= 0:
            continue

        pagina_ancho, pagina_alto = tamanos.get(imagen.pagina, (612.0, 792.0))
        ancho_pt = imagen.ancho / imagen.xppi * 72
        alto_pt = imagen.alto / imagen.yppi * 72
        if (
            ancho_pt >= pagina_ancho * FRACCION_PAGINA_COMPLETA
            and alto_pt >= pagina_alto * FRACCION_PAGINA_COMPLETA
        ):
            # Página escaneada o fondo a página completa: el texto ya viene del OCR.
            continue

        por_pagina.setdefault(imagen.pagina, []).append(imagen)

    for pagina, lista in por_pagina.items():
        lista.sort(key=lambda imagen: imagen.ancho * imagen.alto, reverse=True)
        por_pagina[pagina] = lista[:MAX_IMAGENES_POR_PAGINA]
    return por_pagina


def _convertir_imagen(origen: Path, destino: Path) -> bool:
    if disponible("convert"):
        resultado = subprocess.run(
            [
                "convert",
                str(origen),
                "-resize",
                f"{MAX_LADO_IMAGEN}x{MAX_LADO_IMAGEN}>",
                "-background",
                "white",
                "-alpha",
                "remove",
                "-alpha",
                "off",
                "-strip",
                "-quality",
                str(CALIDAD_JPEG),
                str(destino),
            ],
            capture_output=True,
        )
        if resultado.returncode == 0 and destino.is_file():
            return True

    # Sin ImageMagick se conserva el PNG original.
    destino = destino.with_suffix(origen.suffix.lower())
    shutil.copy2(origen, destino)
    return destino.is_file()


def extraer_imagenes(
    pdf: Path,
    tamanos: dict[int, tuple[float, float]],
    carpeta: Path,
) -> tuple[list[dict], dict[int, list[str]]]:
    """Extrae figuras y devuelve (metadatos, marcas por página)."""
    if not disponible("pdfimages"):
        return [], {}

    imagenes = listar_imagenes(pdf)
    candidatas = _candidatas(imagenes, tamanos)
    if not candidatas:
        return [], {}

    carpeta.mkdir(parents=True, exist_ok=True)
    metadatos: list[dict] = []
    marcas: dict[int, list[str]] = {}
    por_hash: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="pdf_extract_") as temporal:
        tmp = Path(temporal)
        for pagina in sorted(candidatas):
            prefijo = tmp / f"p{pagina:04d}"
            try:
                ejecutar(
                    [
                        "pdfimages",
                        "-f",
                        str(pagina),
                        "-l",
                        str(pagina),
                        "-png",
                        "-p",
                        str(pdf),
                        str(prefijo),
                    ],
                    f"pdfimages en la página {pagina}",
                )
            except SystemExit as error:
                print(f"  aviso: {error}")
                continue

            for imagen in candidatas[pagina]:
                origen = tmp / f"{prefijo.name}-{pagina:03d}-{imagen.numero:03d}.png"
                if not origen.is_file():
                    coincidencias = sorted(tmp.glob(f"{prefijo.name}-*.png"))
                    if imagen.numero < len(coincidencias):
                        origen = coincidencias[imagen.numero]
                    else:
                        continue

                datos = origen.read_bytes()
                digest = hashlib.sha256(datos).hexdigest()
                if digest in por_hash:
                    relativo = por_hash[digest]
                else:
                    nombre = f"pagina_{pagina:03d}_img_{imagen.numero:03d}.jpg"
                    final = carpeta / nombre
                    if not _convertir_imagen(origen, final):
                        continue
                    if not final.is_file():
                        final = final.with_suffix(origen.suffix.lower())
                    relativo = f"imagenes/{final.name}"
                    por_hash[digest] = relativo

                metadatos.append(
                    {
                        "pagina": pagina,
                        "numero": imagen.numero,
                        "ancho": imagen.ancho,
                        "alto": imagen.alto,
                        "archivo": relativo,
                        "sha256": digest,
                    }
                )
                marcas.setdefault(pagina, []).append(f"<<<IMAGEN: {relativo}>>>")

    return metadatos, marcas


def _recortar(imagen: Path) -> bool:
    """Recorta el margen blanco para que la figura ocupe la imagen completa."""
    if not disponible("convert"):
        return imagen.is_file()
    temporal = imagen.with_name(f"{imagen.stem}_trim{imagen.suffix}")
    resultado = subprocess.run(
        [
            "convert",
            str(imagen),
            "-trim",
            "+repage",
            "-bordercolor",
            "white",
            "-border",
            "12",
            "-strip",
            str(temporal),
        ],
        capture_output=True,
    )
    if resultado.returncode == 0 and temporal.is_file():
        temporal.replace(imagen)
        return True
    return imagen.is_file()


def _es_parrafo(bloque: list[Linea]) -> bool:
    """Un bloque con varias líneas de texto es prosa, no una etiqueta de figura."""
    return sum(len(linea.texto.split()) for linea in bloque) >= 25


@dataclass
class Zona:
    """Recorte de una figura vectorial: la zona entre dos cotas verticales."""

    numero: int
    ancho: float
    alto: float
    arriba: float
    tope: float
    orden: int
    total: int


def zonas_figuras(paginas: list[Pagina], excluir: set[int]) -> list[Zona]:
    """Ubica la zona de cada figura vectorial (TikZ, diagramas, tablas).

    `pdfimages` solo ve imágenes incrustadas. Los diagramas dibujados con
    vectores no aparecen, pero se pueden describir con visión si se renderiza
    la zona que va del último párrafo a la leyenda «Figura N:». Las etiquetas
    del diagrama se quitan del texto: la descripción las sustituye.
    """
    zonas: list[Zona] = []
    for pagina in paginas:
        if pagina.numero in excluir or pagina.alto <= 0:
            continue

        leyendas = [
            bloque for bloque in pagina.bloques if _RE_LEYENDA.match(bloque[0].texto)
        ]
        for orden, leyenda in enumerate(
            sorted(leyendas, key=lambda bloque: bloque[0].y), start=1
        ):
            tope = min(linea.y for linea in leyenda)
            arriba = max(
                (
                    max(linea.y + linea.alto for linea in bloque)
                    for bloque in pagina.bloques
                    if max(linea.y + linea.alto for linea in bloque) <= tope - 1
                    and (
                        _es_parrafo(bloque) or _RE_LEYENDA.match(bloque[0].texto)
                    )
                ),
                default=pagina.alto * 0.05,
            )
            if tope - arriba - 4 < 24:
                continue

            zonas.append(
                Zona(
                    numero=pagina.numero,
                    ancho=pagina.ancho,
                    alto=pagina.alto,
                    arriba=arriba,
                    tope=tope,
                    orden=orden,
                    total=len(leyendas),
                )
            )
    return zonas


def quitar_zonas(paginas: list[Pagina], zonas: list[Zona]) -> list[Pagina]:
    """Descarta las líneas que caen dentro de un recorte de figura."""
    if not zonas:
        return paginas

    por_pagina: dict[int, list[Zona]] = {}
    for zona in zonas:
        por_pagina.setdefault(zona.numero, []).append(zona)

    resultado: list[Pagina] = []
    for pagina in paginas:
        candidatas = por_pagina.get(pagina.numero)
        if not candidatas:
            resultado.append(pagina)
            continue

        bloques: list[list[Linea]] = []
        for bloque in pagina.bloques:
            conservadas = [
                linea
                for linea in bloque
                if not any(
                    zona.arriba - 1 <= linea.y < zona.tope - 1 for zona in candidatas
                )
            ]
            if conservadas:
                bloques.append(conservadas)

        resultado.append(
            Pagina(
                numero=pagina.numero,
                ancho=pagina.ancho,
                alto=pagina.alto,
                bloques=bloques,
            )
        )
    return resultado


def recortar_figuras(pdf: Path, zonas: list[Zona], carpeta: Path) -> tuple[list[dict], dict[int, list[str]]]:
    """Renderiza los recortes de las figuras vectoriales a 150 ppp."""
    if not zonas or not disponible("pdftoppm") or not disponible("convert"):
        return [], {}

    metadatos: list[dict] = []
    marcas: dict[int, list[str]] = {}
    carpeta.mkdir(parents=True, exist_ok=True)

    escala = DPI_FIGURA / 72
    for zona in zonas:
        x = int(zona.ancho * 0.04 * escala)
        y = int(max(0.0, zona.arriba - 2) * escala)
        ancho_px = int(zona.ancho * 0.92 * escala)
        alto_px = int((zona.tope - zona.arriba - 4) * escala)
        if ancho_px < 40 or alto_px < 40:
            continue

        sufijo = "" if zona.total == 1 else f"_{zona.orden:02d}"
        base = f"pagina_{zona.numero:03d}_figura{sufijo}"
        destino = carpeta / f"{base}.jpg"
        temporal = carpeta / f"{base}.png"
        try:
            ejecutar(
                [
                    "pdftoppm",
                    "-f",
                    str(zona.numero),
                    "-l",
                    str(zona.numero),
                    "-r",
                    str(DPI_FIGURA),
                    "-x",
                    str(x),
                    "-y",
                    str(y),
                    "-W",
                    str(ancho_px),
                    "-H",
                    str(alto_px),
                    "-png",
                    "-singlefile",
                    str(pdf),
                    str(temporal.with_suffix("")),
                ],
                f"pdftoppm en la página {zona.numero}",
            )
        except SystemExit as error:
            print(f"  aviso: {error}")
            continue

        if not temporal.is_file():
            continue
        if not _convertir_imagen(temporal, destino):
            temporal.unlink(missing_ok=True)
            continue
        temporal.unlink(missing_ok=True)
        _recortar(destino)

        digest = hashlib.sha256(destino.read_bytes()).hexdigest()
        relativo = f"imagenes/{destino.name}"
        metadatos.append(
            {
                "pagina": zona.numero,
                "numero": None,
                "tipo": "vectorial",
                "archivo": relativo,
                "sha256": digest,
            }
        )
        marcas.setdefault(zona.numero, []).append(f"<<<IMAGEN: {relativo}>>>")

    return metadatos, marcas


# ---------------------------------------------------------------------------
# Procesamiento de un PDF
# ---------------------------------------------------------------------------


def _insertar_marcas(parrafos: list[str], marcas: list[str]) -> list[str]:
    """Coloca las marcas de imagen antes de su leyenda o al final de la página."""
    if not marcas:
        return parrafos

    indice = next(
        (posicion for posicion, parrafo in enumerate(parrafos) if _RE_LEYENDA.match(parrafo)),
        len(parrafos),
    )
    return parrafos[:indice] + list(marcas) + parrafos[indice:]


def extraer(
    pdf: Path,
    forzar: bool = False,
    sin_ocr: bool = False,
    sin_imagenes: bool = False,
) -> bool:
    # El PDF con OCR vive en un directorio temporal que debe durar hasta el
    # final del procesamiento: la estructura también se lee de ese archivo.
    with tempfile.TemporaryDirectory(prefix="pdf_tmp_") as temporal:
        return _procesar(pdf, Path(temporal), forzar, sin_ocr, sin_imagenes)


def _procesar(
    pdf: Path,
    temporal: Path,
    forzar: bool = False,
    sin_ocr: bool = False,
    sin_imagenes: bool = False,
) -> bool:
    destino = DESTINO / pdf.stem

    if destino.exists():
        if not forzar:
            print(f"Omitido: {destino} ya existe (usa --force para reemplazarlo)")
            return False
        shutil.rmtree(destino)
    destino.mkdir(parents=True, exist_ok=True)

    info = info_pdf(pdf)
    total = info["paginas"]
    titulo = info["titulo"] or pdf.stem

    texto, motor = texto_plano(pdf)
    paginas_texto = paginas_de_texto(texto, total)
    if total == 0:
        total = len(paginas_texto)

    vacias = paginas_sin_texto(paginas_texto)
    ocr_paginas: list[int] = []
    pdf_lectura = pdf

    if vacias and not sin_ocr:
        ocr = aplicar_ocr(pdf, vacias, temporal)
        if ocr:
            texto_ocr, motor_ocr = texto_plano(ocr)
            paginas_texto = paginas_de_texto(texto_ocr, total)
            pdf_lectura = ocr
            motor = f"{motor}+ocrmypdf ({motor_ocr})"
            ocr_paginas = vacias
            print(f"  OCR aplicado en {len(vacias)} página(s)")

    # La estructura se lee del PDF con OCR; las figuras, del PDF original.
    paginas_bbox = []
    if disponible("pdftotext"):
        try:
            paginas_bbox = _bbox(pdf_lectura)
        except SystemExit as error:
            print(f"  aviso: {error}")

    tamanos: dict[int, tuple[float, float]] = {
        pagina.numero: (pagina.ancho, pagina.alto) for pagina in paginas_bbox
    } or info["tamanos"]

    metadatos: list[dict] = []
    marcas: dict[int, list[str]] = {}
    zonas: list[Zona] = []

    if not sin_imagenes:
        metadatos, marcas = extraer_imagenes(pdf, tamanos, destino / "imagenes")
        if paginas_bbox:
            # Las páginas con imagen raster o con OCR no se recortan otra vez.
            zonas = zonas_figuras(paginas_bbox, set(marcas) | set(ocr_paginas))

    # Las etiquetas del diagrama se quitan: la descripción con visión las
    # sustituye y el texto suelto solo aportaría fragmentos sin sentido.
    estructura = quitar_zonas(paginas_bbox, zonas) if zonas else paginas_bbox

    if estructura:
        parrafos = parrafos_por_pagina(estructura)
    else:
        parrafos = parrafos_de_texto_plano(paginas_texto)

    if zonas:
        vectoriales, marcas_vectoriales = recortar_figuras(
            pdf, zonas, destino / "imagenes"
        )
        metadatos.extend(vectoriales)
        for numero, marcas_pagina in marcas_vectoriales.items():
            marcas.setdefault(numero, []).extend(marcas_pagina)

    for indice, marcas_pagina in marcas.items():
        if 1 <= indice <= len(parrafos):
            parrafos[indice - 1] = _insertar_marcas(
                parrafos[indice - 1], marcas_pagina
            )

    # Rellenar páginas que no produjeron párrafos para conservar la numeración.
    while len(parrafos) < total:
        parrafos.append([])
    parrafos = parrafos[:total]

    contenido_paginas = ["\n\n".join(pagina).strip() for pagina in parrafos]
    # Cada página empieza en su propia línea para que la marca de imagen no se
    # pegue al primer párrafo de la página siguiente.
    texto_final = "\n\f\n".join(contenido_paginas)
    if not texto_final.strip():
        shutil.rmtree(destino)
        raise SystemExit(
            f"No se extrajo texto de {pdf.name}. ¿Es un PDF escaneado sin OCR?"
        )

    (destino / "texto.txt").write_text(texto_final + "\n", encoding="utf-8")

    palabras = sum(len(pagina.split()) for pagina in contenido_paginas)
    manifiesto = {
        "libro": pdf.stem,
        "archivo": pdf.name,
        "titulo": titulo,
        "paginas": total,
        "paginas_con_texto": sum(1 for pagina in contenido_paginas if pagina),
        "motor_texto": motor,
        "ocr": bool(ocr_paginas),
        "paginas_ocr": ocr_paginas,
        "palabras": palabras,
        "imagenes": metadatos,
        "salida": "texto.txt",
    }
    (destino / "manifiesto.json").write_text(
        json.dumps(manifiesto, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Extraído: {pdf.name} -> {destino}")
    print(
        f"  {total} páginas · {palabras} palabras · "
        f"{len(metadatos)} figura(s)"
        + (f" · OCR en {len(ocr_paginas)} página(s)" if ocr_paginas else "")
    )
    print(f"  Texto: {destino / 'texto.txt'}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extrae PDF de src/ a texto plano en texto/<nombre>/."
    )
    parser.add_argument(
        "pdf",
        nargs="?",
        help="Archivo o nombre dentro de src/. Si se omite, procesa todos.",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Reemplaza la extracción si la carpeta ya existe.",
    )
    parser.add_argument(
        "--sin-ocr",
        action="store_true",
        help="No intenta OCR en páginas sin capa de texto.",
    )
    parser.add_argument(
        "--sin-imagenes",
        action="store_true",
        help="No extrae figuras.",
    )
    args = parser.parse_args()

    ORIGEN.mkdir(parents=True, exist_ok=True)
    DESTINO.mkdir(parents=True, exist_ok=True)

    if args.pdf:
        archivos = [resolver(args.pdf)]
    else:
        archivos = pdfs_disponibles()
        if not archivos:
            print(f"No hay archivos .pdf en {ORIGEN}")
            return

    for pdf in archivos:
        extraer(
            pdf,
            forzar=args.force,
            sin_ocr=args.sin_ocr,
            sin_imagenes=args.sin_imagenes,
        )


if __name__ == "__main__":
    main()
