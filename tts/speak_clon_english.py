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

# Referencia inglesa nativa generada con VoiceDesign y congelada como voz.
REFERENCIA = Path("referencia_narradora_english.wav")
NOMBRE_VOZ = "narradora_english"
TRANSCRIPCION = (
    "Morning light settled over the city as the streets began to fill with the sound "
    "of traffic and distant conversation. She opened the notebook, read the first line, "
    "and understood that the day would ask more of her than she had planned to give."
)

SEED = 12345
IDIOMA = "en"
SALIDA = "clon_narradora_english.mp3"

TEXTO = (
    "The archive room smelled of dust and old paper. She set the lamp on the table "
    "and began to read the letters in order, one by one, until the names stopped "
    "being names and became people she could almost hear. Outside, the rain kept "
    "falling on the glass. Somewhere in the building a door closed, and the sound "
    "travelled down the empty hall like a sentence that had been waiting years "
    "to be finished."
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
            headers={**cabeceras(), "Content-Type": "audio/wav"},
            timeout=300,
        )
    respuesta.raise_for_status()

    respuesta = requests.post(
        f"{BASE}/v1/audio/voices",
        json={
            "source": {"input_id": respuesta.json()["id"]},
            "name": NOMBRE_VOZ,
            "transcript": TRANSCRIPCION,
            "language": "English",
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
