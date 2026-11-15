import os
import requests
from pathlib import Path

BASE = os.environ.get("UNSLOTH_BASE", "http://localhost:8888")
# La clave no se versiona: va en UNSLOTH_API_KEY o en .unsloth.key junto al script.
KEY = os.environ.get("UNSLOTH_API_KEY", "").strip()
if not KEY:
    _archivo_clave = Path(__file__).resolve().parent / ".unsloth.key"
    KEY = _archivo_clave.read_text(encoding="utf-8").strip() if _archivo_clave.is_file() else ""
SEED = 12345  # same seed + same instructions = same voice

# Free English translation of the Spanish sample in speak_v.0.2.py.
TEXTO = """ Waking was a jolt of pure adrenaline. Andromache opened her eyes and the world snapped into focus on two pools of radioactive emerald staring at her from inches away. Sunar was there, leaning over the mercury bed, utterly motionless, like a gargoyle carved from obsidian. His presence was a mass of cold and menace that cut through the perfumed air of the room. Andromache let out a strangled gasp, her body reacting with the speed of a cornered animal as her hands instinctively searched for a weapon she did not have."""


VOCES = {
    # Gender is declared twice ("clearly a male voice, a man speaking") because
    # long literary text can override the designed voice's gender.
    "documentary_narrator": (
        "Man in his forties, clearly a male voice, a man speaking, "
        "deep resonant baritone, calm and authoritative tone, "
        "steady moderate pacing, documentary narrator."
    ),
    "female_narrator": (
        "Woman in her thirties, clearly a female voice, a woman speaking, "
        "native English speaker with a neutral American accent, warm and friendly, "
        "medium pitch, clear articulation, relaxed pacing, conversational style, "
        "natural English intonation."
    ),
    "warm_assistant": (
        "Woman in her thirties, clearly a female voice, a woman speaking, "
        "warm and friendly mid-range voice, clear articulation, "
        "slightly upbeat tone, conversational style."
    ),
    "dramatic_character": (
        "Older man, clearly a male voice, a man speaking, "
        "raspy and gravelly, slow mysterious pacing, low pitch, "
        "storytelling narrator."
    ),
    # Production voice. This is the description used to generate
    # referencia_narradora_english.wav, which is then cloned with the Base
    # model (speak_clon_english.py) to keep the timbre fixed for the book.
    "narradora_english": (
        "Woman in her thirties, clearly a female voice, a woman speaking, "
        "native English speaker with a neutral American accent, calm and professional "
        "audiobook narrator, medium pitch, clear articulation, steady measured pacing, "
        "restrained warmth, natural English intonation."
    ),
}

for nombre, instructions in VOCES.items():
    payload = {
        "model": "audio-cpp/audio.cpp-gguf/Qwen3-TTS-12Hz-1.7B-VoiceDesign-GGUF",
        "input": TEXTO,
        # The Unsloth API uses "instructions".
        # "instruct" belongs to the qwen-tts library and is ignored here.
        "instructions": instructions,
        "language": "en",
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

    salida = f"muestra_english_{nombre}.mp3"
    with open(salida, "wb") as f:
        f.write(response.content)

    print(f"Listo: {salida} (seed={SEED}, {len(response.content)} bytes)")
