import os
import requests
from pathlib import Path

BASE = os.environ.get("UNSLOTH_BASE", "http://localhost:8888")
# La clave no se versiona: va en UNSLOTH_API_KEY o en .unsloth.key junto al script.
KEY = os.environ.get("UNSLOTH_API_KEY", "").strip()
if not KEY:
    _archivo_clave = Path(__file__).resolve().parent / ".unsloth.key"
    KEY = _archivo_clave.read_text(encoding="utf-8").strip() if _archivo_clave.is_file() else ""
SEED = 12345  # misma semilla + mismas instrucciones = misma voz

# "Ansloth" evita que el modelo pronuncie la marca como palabra inglesa.
# TEXTO = "Hola, esta es una prueba de síntesis de voz en español."

TEXTO = """ El despertar fue un latigazo de adrenalina pura. Andrómaca abrió los ojos y el mundo se enfocó en dos pozos de esmeralda radiactiva que la observaban a escasos centímetros de su rostro. Sunar estaba allí, inclinado sobre el lecho de mercurio, con la inmovilidad absoluta de una gárgola de obsidiana. Su presencia era una masa de frío y amenaza que cortaba el aire perfumado de la habitación. Andrómaca soltó un jadeo ahogado, su cuerpo reaccionando con la velocidad de un animal acorralado, mientras sus manos buscaban instintivamente un arma que no tenía."""


VOCES = {
    # El género se declara dos veces ("voz claramente masculina de hombre,
    # hombre hablando") porque el texto literario largo puede imponer su
    # propio género al diseño de voz.
    "narrador_documental": (
        "Hombre de unos 40 años, voz claramente masculina de hombre, hombre hablando, "
        "barítono grave y resonante, tono sereno y con autoridad, "
        "ritmo constante y moderado, narrador de documentales."
    ),
    "narrador_femenino": (
        "Mujer de unos 30 años, voz claramente femenina de mujer, mujer hablando, "
        "narradora nativa de español con acento latino neutro, cálida y amable, "
        "tono medio, articulación clara, ritmo tranquilo, estilo conversacional, "
        "entonación natural del español."
    ),
    "asistente_cercano": (
        "Mujer de unos 30 años, voz claramente femenina de mujer, mujer hablando, "
        "cálida y amable de rango medio, articulación clara, "
        "tono ligeramente alegre, estilo conversacional."
    ),
    "personaje_dramatico": (
        "Hombre mayor, voz claramente masculina de hombre, hombre hablando, "
        "áspera y rasposa, ritmo lento y misterioso, tono grave, "
        "estilo de narrador de historias."
    ),
}

for nombre, instructions in VOCES.items():
    payload = {
        "model": "audio-cpp/audio.cpp-gguf/Qwen3-TTS-12Hz-1.7B-VoiceDesign-GGUF",
        "input": TEXTO,
        # En la API de Unsloth el campo se llama "instructions".
        # "instruct" es de la librería qwen-tts y aquí se ignora.
        "instructions": instructions,
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

    salida = f"muestra_{nombre}.mp3"
    with open(salida, "wb") as f:
        f.write(response.content)

    print(f"Listo: {salida} (seed={SEED}, {len(response.content)} bytes)")
