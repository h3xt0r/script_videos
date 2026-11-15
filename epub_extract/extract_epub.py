"""Extrae un EPUB en html/<nombre>/ conservando su estructura interna.

Uso:
    python extract_epub.py                  # procesa todos los .epub de src/
    python extract_epub.py libro.epub       # procesa solo ese archivo
    python extract_epub.py libro -f         # sobrescribe la extracción previa

La estructura del EPUB se conserva tal cual (META-INF, OEBPS, XHTML, CSS,
imágenes) para que los enlaces relativos sigan funcionando.
"""

from __future__ import annotations

import argparse
import shutil
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree

RAIZ = Path(__file__).resolve().parent
ORIGEN = RAIZ / "src"
DESTINO = RAIZ / "html"

# Basura típica de archivos comprimidos en macOS o Windows.
IGNORAR = {"__MACOSX", ".DS_Store", "Thumbs.db", "desktop.ini"}


def es_ruta_segura(nombre: str) -> bool:
    """Evita entradas absolutas o con .. que escribirían fuera del destino."""
    ruta = Path(nombre)
    return not ruta.is_absolute() and ".." not in ruta.parts


def raiz_opf(comprimido: zipfile.ZipFile) -> str | None:
    """Lee META-INF/container.xml y devuelve la ruta del archivo .opf."""
    try:
        datos = comprimido.read("META-INF/container.xml")
    except KeyError:
        return None

    try:
        arbol = ElementTree.fromstring(datos)
    except ElementTree.ParseError:
        return None

    for elemento in arbol.iter():
        if elemento.tag.endswith("rootfile"):
            return elemento.get("full-path")

    return None


def resumen(extensiones: Counter[str], archivos: int, tamano: int) -> str:
    lineas = [f"  archivos: {archivos}  ({tamano / 1024 / 1024:.1f} MB)"]
    for extension, cantidad in sorted(extensiones.items()):
        lineas.append(f"  {extension:>16}: {cantidad}")
    return "\n".join(lineas)


def extraer(epub: Path, forzar: bool = False) -> bool:
    """Extrae un EPUB. Devuelve True si lo hizo, False si lo omitió."""
    destino = DESTINO / epub.stem

    if destino.exists():
        if not forzar:
            print(f"Omitido: {destino} ya existe (usa --force para reemplazarlo)")
            return False
        shutil.rmtree(destino)

    destino.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(epub) as comprimido:
        inseguras = [n for n in comprimido.namelist() if not es_ruta_segura(n)]
        if inseguras:
            shutil.rmtree(destino)
            raise SystemExit(
                f"EPUB rechazado: contiene rutas fuera del destino: {inseguras[:5]}"
            )

        if "mimetype" not in comprimido.namelist():
            print("  aviso: no tiene entrada 'mimetype'; puede no ser un EPUB válido")

        opf = raiz_opf(comprimido)
        archivos = 0
        tamano = 0
        extensiones: Counter[str] = Counter()

        for info in comprimido.infolist():
            if info.is_dir():
                continue

            partes = Path(info.filename).parts
            if any(parte in IGNORAR for parte in partes):
                continue

            salida = destino.joinpath(*partes)
            salida.parent.mkdir(parents=True, exist_ok=True)

            with comprimido.open(info) as entrada, salida.open("wb") as archivo:
                shutil.copyfileobj(entrada, archivo)

            archivos += 1
            tamano += info.file_size
            extensiones[Path(info.filename).suffix.lower() or "(sin extensión)"] += 1

    print(f"Extraído: {epub.name} -> {destino}")
    if opf:
        print(f"  OPF: {opf}")
    print(resumen(extensiones, archivos, tamano))
    return True


def resolver(nombre: str) -> Path:
    """Acepta una ruta o un nombre dentro de src/, con o sin extensión."""
    ruta = Path(nombre).expanduser()
    if ruta.is_file():
        return ruta.resolve()

    for candidata in (
        ORIGEN / nombre,
        ORIGEN / f"{nombre}.epub",
        ORIGEN / f"{nombre}.EPUB",
    ):
        if candidata.is_file():
            return candidata

    raise SystemExit(f"No encuentro el EPUB: {nombre}")


def epubs_disponibles() -> list[Path]:
    return sorted(
        ruta
        for ruta in ORIGEN.iterdir()
        if ruta.is_file() and ruta.suffix.lower() == ".epub"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extrae EPUB de src/ en html/<nombre>/."
    )
    parser.add_argument(
        "epub",
        nargs="?",
        help="Archivo o nombre dentro de src/. Si se omite, procesa todos.",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Reemplaza la extracción si la carpeta ya existe.",
    )
    args = parser.parse_args()

    ORIGEN.mkdir(parents=True, exist_ok=True)
    DESTINO.mkdir(parents=True, exist_ok=True)

    if args.epub:
        archivos = [resolver(args.epub)]
    else:
        archivos = epubs_disponibles()
        if not archivos:
            print(f"No hay archivos .epub en {ORIGEN}")
            return

    for epub in archivos:
        extraer(epub, forzar=args.force)


if __name__ == "__main__":
    main()
