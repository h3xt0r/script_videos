from pathlib import Path

import os
import requests

BASE = os.environ.get("UNSLOTH_BASE", "http://localhost:8888")
# La clave no se versiona: va en UNSLOTH_API_KEY o en .unsloth.key junto al script.
KEY = os.environ.get("UNSLOTH_API_KEY", "").strip()
if not KEY:
    _archivo_clave = Path(__file__).resolve().parent / ".unsloth.key"
    KEY = _archivo_clave.read_text(encoding="utf-8").strip() if _archivo_clave.is_file() else ""

# El modelo Base es el único que clona voces. El 0.6B-Base da menos calidad.
MODELO = "audio-cpp/audio.cpp-gguf/Qwen3-TTS-12Hz-1.7B-Base-GGUF"

# La voz de referencia se registra una sola vez y se reutiliza por voice_id.
REFERENCIA = Path("consistencia_b.mp3")
NOMBRE_VOZ = "narradora_consistencia_b"
TRANSCRIPCION = (
    "En el capítulo de hoy hablaremos sobre la inteligencia artificial "
    "y su impacto en la vida cotidiana."
)

SEED = 12345
IDIOMA = "es"  # "en" para inglés, "auto" para detectar el idioma
SALIDA = "clon_consistencia_b.mp3"

TEXTO = (
    "El despertar fue un latigazo de adrenalina pura. Andrómaca abrió los ojos "
    "y el mundo se enfocó en dos pozos de esmeralda radiactiva que la observaban "
    "a escasos centímetros de su rostro. Sunar estaba allí, inclinado sobre el "
    "lecho de mercurio, con la inmovilidad absoluta de una gárgola de obsidiana. "
    "Su presencia era una masa de frío y amenaza que cortaba el aire perfumado "
    "de la habitación."
)


def cabeceras():
    return {"Authorization": f"Bearer {KEY}"}


def voz_registrada():
    """Devuelve el id de la voz; la registra si todavía no existe."""
    respuesta = requests.get(f"{BASE}/v1/audio/voices", headers=cabeceras(), timeout=60)
    respuesta.raise_for_status()

    for voz in respuesta.json()["voices"]:
        if voz["name"] == NOMBRE_VOZ and voz["transcript"] == TRANSCRIPCION:
            return voz["id"]

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


payload = {
    "model": MODELO,
    "input": TEXTO,
    "reference": {"voice_id": voz_registrada()},
    # La transcripción es la del audio de referencia, no la del texto a generar.
    "reference_text": TRANSCRIPCION,
    "language": IDIOMA,
    "seed": SEED,
    "response_format": "mp3",
}

response = requests.post(
    f"{BASE}/v1/audio/speech",
    json=payload,
    headers=cabeceras(),
    timeout=900,
)
response.raise_for_status()

with open(SALIDA, "wb") as f:
    f.write(response.content)

print(f"Listo: {SALIDA} ({len(response.content)} bytes)")
