"""Genera el audiolibro a partir de los bloques de texto preparados.

Lee `bloque_*.txt`, divide cada bloque en fragmentos cortos (el TTS degenera
si una petición supera ~170 s de audio), sintetiza cada fragmento con la voz
clonada y arma el MP3 del bloque. Al final une los bloques sin recomprimir.

Uso:
    python generar_audiolibro.py
    python generar_audiolibro.py "2026-11-12-Boletin-09: Deficit comercial mex-usa"
    python generar_audiolibro.py /ruta/a/preparado/libro --forzar
    python generar_audiolibro.py ... --sin-unir
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

import os
import requests

# --- Conexión con Unsloth Studio ---
BASE = os.environ.get("UNSLOTH_BASE", "http://localhost:8888")
# La clave no se versiona: va en UNSLOTH_API_KEY o en .unsloth.key junto al script.
KEY = os.environ.get("UNSLOTH_API_KEY", "").strip()
if not KEY:
    _archivo_clave = Path(__file__).resolve().parent / ".unsloth.key"
    KEY = _archivo_clave.read_text(encoding="utf-8").strip() if _archivo_clave.is_file() else ""

# --- Modelo y voz de producción ---
MODELO = "audio-cpp/audio.cpp-gguf/Qwen3-TTS-12Hz-1.7B-Base-GGUF"
REFERENCIA = Path(__file__).resolve().parent / "consistencia_b.mp3"
NOMBRE_VOZ = "narradora_consistencia_b"
TRANSCRIPCION = (
    "En el capítulo de hoy hablaremos sobre la inteligencia artificial "
    "y su impacto en la vida cotidiana."
)
IDIOMA = "es"
SEED = 12345
FORMATO = "mp3"

# --- Estructura del proyecto ---
RAIZ = Path(__file__).resolve().parent
PREPARADO_DIR = RAIZ.parent / "epub_extract" / "preparado"
AUDIO_DIR = RAIZ / "audiolibros"

# --- Parámetros de generación ---
REINTENTOS = 3
TIMEOUT = 3600
# Solo se usa si el manifiesto preparado no trae minutos estimados.
PALABRAS_POR_MINUTO = 160
# El TTS degenera en repeticiones y ruido cuando una petición supera ~170 s de
# audio (2,048 frames a 12 Hz). Con 250 palabras el fragmento queda en ~2 min.
MAX_PALABRAS_POR_CHUNK = 250
# Tope duro de audio por petición: 158 s. Evita que una petición se desborde.
MAX_NEW_TOKENS = 1900


def cabeceras() -> dict[str, str]:
    return {"Authorization": f"Bearer {KEY}"}


def elegir_libro(nombre: str | None) -> Path:
    """Acepta una ruta o un nombre dentro de epub_extract/preparado."""
    if nombre:
        ruta = Path(nombre).expanduser()
        if ruta.is_dir() and any(ruta.glob("bloque_*.txt")):
            return ruta.resolve()

        candidatos = sorted(
            carpeta
            for carpeta in PREPARADO_DIR.iterdir()
            if carpeta.is_dir()
            and carpeta.name != "cache"
            and any(carpeta.glob("bloque_*.txt"))
        )
        exactos = [carpeta for carpeta in candidatos if carpeta.name == nombre]
        if exactos:
            return exactos[0]
        parciales = [
            carpeta for carpeta in candidatos if nombre.lower() in carpeta.name.lower()
        ]
        if len(parciales) == 1:
            return parciales[0]
        if not parciales:
            raise SystemExit(f"No encuentro el libro preparado: {nombre}")
        listado = "\n".join(f"  - {carpeta.name}" for carpeta in parciales)
        raise SystemExit(f"El nombre coincide con varios libros:\n{listado}")

    candidatos = sorted(
        carpeta
        for carpeta in PREPARADO_DIR.iterdir()
        if carpeta.is_dir()
        and carpeta.name != "cache"
        and any(carpeta.glob("bloque_*.txt"))
    )
    if not candidatos:
        raise SystemExit(
            f"No hay libros preparados en {PREPARADO_DIR}. "
            "Ejecuta generar_texto_para_audio.py primero."
        )
    if len(candidatos) == 1:
        return candidatos[0]
    listado = "\n".join(f"  - {carpeta.name}" for carpeta in candidatos)
    raise SystemExit(f"Hay varios libros preparados. Indica uno:\n{listado}")


def bloques(libro: Path) -> list[Path]:
    rutas = list(libro.glob("bloque_*.txt"))

    def numero(ruta: Path) -> int:
        coincidencia = re.search(r"(\d+)", ruta.stem)
        return int(coincidencia.group(1)) if coincidencia else 0

    return sorted(rutas, key=numero)


def modelo_cargado() -> bool:
    respuesta = requests.get(f"{BASE}/v1/models", headers=cabeceras(), timeout=60)
    respuesta.raise_for_status()
    for modelo in respuesta.json().get("data", []):
        if modelo.get("id") == MODELO:
            return bool(modelo.get("loaded"))
    return False


def cargar_modelo() -> None:
    if modelo_cargado():
        return

    print(f"Cargando {MODELO}...")
    respuesta = requests.post(
        f"{BASE}/api/inference/load",
        json={"model_path": MODELO},
        headers=cabeceras(),
        timeout=1800,
    )
    if respuesta.status_code == 200:
        return

    # El modelo de texto (Gemma) puede ocupar la VRAM que necesita el TTS.
    if respuesta.status_code == 400 and "out_of_memory" in respuesta.text:
        liberar_otros_modelos()
        respuesta = requests.post(
            f"{BASE}/api/inference/load",
            json={"model_path": MODELO},
            headers=cabeceras(),
            timeout=1800,
        )
        if respuesta.status_code == 200:
            return

    raise SystemExit(
        f"No se pudo cargar el modelo TTS (HTTP {respuesta.status_code}): "
        f"{respuesta.text[:600]}"
    )


def liberar_otros_modelos() -> None:
    """Descarga los modelos que no sean el TTS para liberar VRAM."""
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


def voz_existente() -> str | None:
    respuesta = requests.get(f"{BASE}/v1/audio/voices", headers=cabeceras(), timeout=60)
    respuesta.raise_for_status()
    for voz in respuesta.json()["voices"]:
        if voz["name"] == NOMBRE_VOZ and voz["transcript"] == TRANSCRIPCION:
            return voz["id"]
    return None


def voz_registrada() -> str:
    """Devuelve el id de la voz; la registra si todavía no existe."""
    existente = voz_existente()
    if existente:
        return existente

    if not REFERENCIA.is_file():
        raise SystemExit(f"Falta la referencia de voz: {REFERENCIA}")

    with REFERENCIA.open("rb") as archivo:
        respuesta = requests.post(
            f"{BASE}/v1/audio/inputs",
            params={"name": REFERENCIA.stem},
            data=archivo,
            headers={**cabeceras(), "Content-Type": "audio/mpeg"},
            timeout=300,
        )
    respuesta.raise_for_status()

    respuesta = requests.post(
        f"{BASE}/v1/audio/voices",
        json={
            "source": {"input_id": respuesta.json()["id"]},
            "name": NOMBRE_VOZ,
            "transcript": TRANSCRIPCION,
            "language": "Spanish",
        },
        headers=cabeceras(),
        timeout=300,
    )
    respuesta.raise_for_status()
    return respuesta.json()["id"]


def dividir_texto(texto: str, limite: int = MAX_PALABRAS_POR_CHUNK) -> list[str]:
    """Divide el texto en fragmentos que el TTS sintetiza sin degenerar.

    Respeta párrafos y, si uno es demasiado largo, corta por frases. Nunca
    parte una frase por la mitad.
    """
    parrafos = [p.strip() for p in re.split(r"\n\s*\n", texto) if p.strip()]
    unidades: list[str] = []

    for parrafo in parrafos:
        if len(parrafo.split()) <= limite:
            unidades.append(parrafo)
            continue

        actual: list[str] = []
        cuenta = 0
        for frase in re.split(r"(?<=[.!?…])\s+", parrafo):
            peso = len(frase.split())
            if actual and cuenta + peso > limite:
                unidades.append(" ".join(actual))
                actual = []
                cuenta = 0
            actual.append(frase)
            cuenta += peso
        if actual:
            unidades.append(" ".join(actual))

    trozos: list[str] = []
    actual = []
    cuenta = 0
    for unidad in unidades:
        peso = len(unidad.split())
        if actual and cuenta + peso > limite:
            trozos.append("\n\n".join(actual))
            actual = []
            cuenta = 0
        actual.append(unidad)
        cuenta += peso
    if actual:
        trozos.append("\n\n".join(actual))
    return trozos


def generar_parte(texto: str, voice_id: str, destino: Path) -> int:
    payload = {
        "model": MODELO,
        "input": texto,
        "reference": {"voice_id": voice_id},
        # La transcripción es la del audio de referencia, no la del texto.
        "reference_text": TRANSCRIPCION,
        "language": IDIOMA,
        "seed": SEED,
        "response_format": FORMATO,
        "max_new_tokens": MAX_NEW_TOKENS,
    }

    for intento in range(REINTENTOS):
        try:
            respuesta = requests.post(
                f"{BASE}/v1/audio/speech",
                json=payload,
                headers=cabeceras(),
                timeout=TIMEOUT,
            )
        except requests.RequestException as error:
            if intento == REINTENTOS - 1:
                raise SystemExit(f"Error de conexión con el TTS: {error}")
            time.sleep(2**intento)
            continue

        if respuesta.status_code == 200:
            temporal = destino.with_suffix(destino.suffix + ".tmp")
            temporal.write_bytes(respuesta.content)
            temporal.replace(destino)
            return len(respuesta.content)

        if respuesta.status_code >= 500 and intento < REINTENTOS - 1:
            time.sleep(2**intento)
            continue
        raise SystemExit(
            f"El TTS devolvió HTTP {respuesta.status_code}: {respuesta.text[:600]}"
        )

    return 0


def generar_bloque(
    texto: str,
    voice_id: str,
    destino: Path,
    partes_dir: Path,
    forzar: bool = False,
) -> tuple[int, int]:
    """Sintetiza un bloque por fragmentos y lo arma sin recomprimir.

    Devuelve (bytes del bloque, número de fragmentos).
    """
    trozos = dividir_texto(texto)

    if len(trozos) <= 1:
        if forzar or not destino.is_file() or destino.stat().st_size == 0:
            generar_parte(texto, voice_id, destino)
        return destino.stat().st_size, 1

    partes_dir.mkdir(parents=True, exist_ok=True)
    partes: list[Path] = []
    for indice, trozo in enumerate(trozos, start=1):
        parte = partes_dir / f"{destino.stem}_{indice:03d}.mp3"
        if forzar:
            parte.unlink(missing_ok=True)
        if parte.is_file() and parte.stat().st_size > 0:
            print(f"  fragmento {indice}/{len(trozos)}: ya existe; se omite")
        else:
            print(
                f"  fragmento {indice}/{len(trozos)}: "
                f"{len(trozo.split())} palabras"
            )
            generar_parte(trozo, voice_id, parte)
        partes.append(parte)

    unir(partes, destino, destino.stem)
    return destino.stat().st_size, len(partes)


def duracion(ruta: Path) -> float | None:
    try:
        resultado = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(ruta),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return round(float(resultado.stdout.strip()), 2)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def _escapar_concat(ruta: Path) -> str:
    return str(ruta.resolve()).replace("'", "'\\''")


def unir(partes: list[Path], destino: Path, titulo: str) -> None:
    """Une los MP3 sin recodificar. Los bloques originales no se tocan."""
    if not partes:
        return

    lista = destino.parent / "concat.txt"
    lista.write_text(
        "\n".join(f"file '{_escapar_concat(parte)}'" for parte in partes) + "\n",
        encoding="utf-8",
    )

    comando = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(lista),
        "-c",
        "copy",
        "-id3v2_version",
        "3",
        "-metadata",
        f"title={titulo}",
        str(destino),
    ]
    try:
        subprocess.run(comando, check=True)
    except FileNotFoundError:
        raise SystemExit("Falta ffmpeg para unir los bloques.")
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            f"ffmpeg no pudo unir los bloques (código {error.returncode}). "
            "Los MP3 por bloque siguen disponibles."
        )
    finally:
        lista.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Genera el audiolibro desde los bloques preparados."
    )
    parser.add_argument("libro", nargs="?", help="Ruta o nombre del libro preparado.")
    parser.add_argument(
        "--salida",
        help="Carpeta de salida (por defecto tts/audiolibros/<libro>).",
    )
    parser.add_argument(
        "--forzar",
        action="store_true",
        help="Vuelve a generar los bloques que ya tienen MP3.",
    )
    parser.add_argument(
        "--sin-unir",
        action="store_true",
        help="Genera solo los MP3 por bloque, sin unirlos.",
    )
    parser.add_argument(
        "--sin-cargar",
        action="store_true",
        help="No carga el modelo TTS; falla si no está listo.",
    )
    args = parser.parse_args()

    libro = elegir_libro(args.libro)
    rutas = bloques(libro)
    if not rutas:
        raise SystemExit(f"No hay bloque_*.txt en {libro}")

    destino = Path(args.salida).expanduser() if args.salida else AUDIO_DIR / libro.name
    destino.mkdir(parents=True, exist_ok=True)

    pendientes = [
        ruta
        for ruta in rutas
        if args.forzar
        or not (destino / f"{ruta.stem}.mp3").is_file()
        or (destino / f"{ruta.stem}.mp3").stat().st_size == 0
    ]

    if pendientes:
        if args.sin_cargar:
            if not modelo_cargado():
                raise SystemExit(
                    f"{MODELO} no está cargado. Cárgalo en Unsloth Studio o "
                    "ejecuta sin --sin-cargar."
                )
        else:
            cargar_modelo()
        voice_id = voz_registrada()
    else:
        voice_id = voz_existente()
        print("Todos los bloques ya existen; no hace falta cargar el TTS.")

    manifiesto_preparado = {}
    ruta_manifiesto = libro / "manifiesto.json"
    if ruta_manifiesto.is_file():
        manifiesto_preparado = json.loads(ruta_manifiesto.read_text(encoding="utf-8"))
    estimados = {
        bloque["archivo"]: bloque
        for bloque in manifiesto_preparado.get("bloques", [])
    }
    titulo = manifiesto_preparado.get("titulo") or libro.name

    print(f"Libro: {libro.name}")
    print(f"Voz: {NOMBRE_VOZ} ({voice_id})")
    print(f"Salida: {destino}")
    print(f"Bloques: {len(rutas)}\n")

    partes: list[Path] = []
    registro: list[dict] = []
    inicio = time.perf_counter()

    for indice, ruta in enumerate(rutas, start=1):
        texto = ruta.read_text(encoding="utf-8").strip()
        if not texto:
            print(f"[{indice}/{len(rutas)}] {ruta.name}: vacío; se omite")
            continue

        salida = destino / f"{ruta.stem}.mp3"
        partes.append(salida)
        datos = estimados.get(ruta.name, {})
        palabras = datos.get("palabras") or len(texto.split())
        minutos = datos.get("minutos_estimados") or palabras / PALABRAS_POR_MINUTO

        if salida.is_file() and salida.stat().st_size > 0 and not args.forzar:
            fragmentos = len(list((destino / "partes").glob(f"{ruta.stem}_*.mp3")))
            print(
                f"[{indice}/{len(rutas)}] {ruta.name}: ya existe "
                f"({salida.stat().st_size} bytes); se omite"
            )
            registro.append(
                {
                    "archivo": salida.name,
                    "palabras": palabras,
                    "minutos_estimados": round(minutos, 2),
                    "duracion_segundos": duracion(salida),
                    "bytes": salida.stat().st_size,
                    "fragmentos": fragmentos or 1,
                    "generado": False,
                }
            )
            continue

        print(
            f"[{indice}/{len(rutas)}] {ruta.name}: {palabras} palabras · "
            f"~{minutos:.1f} min de audio"
        )
        t = time.perf_counter()
        tamano, fragmentos = generar_bloque(
            texto,
            voice_id,
            salida,
            destino / "partes",
            forzar=args.forzar,
        )
        dt = time.perf_counter() - t
        segundos = duracion(salida)
        print(
            f"  listo en {dt:.1f}s · {tamano} bytes · {fragmentos} fragmento(s)"
            + (f" · {segundos / 60:.1f} min" if segundos else "")
        )
        if segundos:
            print(f"  ritmo real: {palabras / (segundos / 60):.0f} palabras/min")
        registro.append(
            {
                "archivo": salida.name,
                "palabras": palabras,
                "minutos_estimados": round(minutos, 2),
                "duracion_segundos": segundos,
                "bytes": tamano,
                "fragmentos": fragmentos,
                "generado": True,
            }
        )

    unido = None
    if not args.sin_unir and len(partes) >= 1:
        unido = destino / "audiolibro.mp3"
        print(f"\nUniendo {len(partes)} bloque(s) sin recomprimir...")
        unir(partes, unido, titulo)
        segundos = duracion(unido)
        print(
            f"Audiolibro: {unido}"
            + (f" · {segundos / 60:.1f} min" if segundos else "")
        )

    manifiesto = {
        "libro": libro.name,
        "titulo": titulo,
        "modelo": MODELO,
        "voz": NOMBRE_VOZ,
        "voice_id": voice_id,
        "idioma": IDIOMA,
        "seed": SEED,
        "formato": FORMATO,
        "bloques": registro,
        "audiolibro": unido.name if unido else None,
        "duracion_audiolibro_segundos": duracion(unido) if unido else None,
    }
    (destino / "manifiesto_audio.json").write_text(
        json.dumps(manifiesto, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    total = sum(item.get("duracion_segundos") or 0 for item in registro)
    print(
        f"\nTotal: {len(registro)} bloque(s) · {total / 60:.1f} min · "
        f"{time.perf_counter() - inicio:.1f}s"
    )


if __name__ == "__main__":
    main()
