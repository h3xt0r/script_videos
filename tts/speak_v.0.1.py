import os
import requests
from pathlib import Path

BASE = os.environ.get("UNSLOTH_BASE", "http://localhost:8888")
# La clave no se versiona: va en UNSLOTH_API_KEY o en .unsloth.key junto al script.
KEY = os.environ.get("UNSLOTH_API_KEY", "").strip()
if not KEY:
    _archivo_clave = Path(__file__).resolve().parent / ".unsloth.key"
    KEY = _archivo_clave.read_text(encoding="utf-8").strip() if _archivo_clave.is_file() else ""
SALIDA = "speech_es.mp3"
SEED = 12345  # misma semilla + mismas instrucciones = misma voz

payload = {
    "model": "audio-cpp/audio.cpp-gguf/Qwen3-TTS-12Hz-1.7B-VoiceDesign-GGUF",
    "input": "Hola, esta es una prueba de síntesis de voz en español.",
    # En la API de Unsloth el campo se llama "instructions".
    # "instruct" es de la librería qwen-tts y aquí se ignora.
    "instructions": (
        "Mujer de unos 30 años, voz claramente femenina de mujer, mujer hablando, "
        "narradora nativa de español con acento latino neutro, cálida y amable, "
        "tono medio, articulación clara, ritmo tranquilo, estilo conversacional, "
        "entonación natural del español."
    ),
    "language": "es",
    "seed": SEED,
    "response_format": "mp3",
}

response = requests.post(
    f"{BASE}/v1/audio/speech",
    json=payload,
    headers={"Authorization": f"Bearer {KEY}"},
    timeout=300,
)
response.raise_for_status()

with open(SALIDA, "wb") as f:
    f.write(response.content)

print(f"Listo: {SALIDA} ({len(response.content)} bytes)")
