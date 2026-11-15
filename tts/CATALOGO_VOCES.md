# Catálogo de voces

Estado: **cerrado**. Las dos voces de producción están registradas en Unsloth
Studio con su transcripción exacta y se reutilizan por `voice_id`.

| Idioma | Voz | ID | Referencia | F0 |
|---|---|---|---|---|
| Español | `narradora_consistencia_b` | `ee3a67f717aa4484be290a666182366d` | `consistencia_b.mp3` (5.68 s) | 163.3 Hz |
| Inglés | `narradora_english` | `b1e61e64ef8d4eeaaf1d93ee67c7dc2f` | `referencia_narradora_english.wav` (13.04 s) | 195.1 Hz |

Registro: tono neutro y profesional, voz femenina.

## Producción

| Idioma | Script | Modelo | Salida |
|---|---|---|---|
| Español | `speak_clon_v.0.1.py` | `Qwen3-TTS-12Hz-1.7B-Base-GGUF` | `clon_consistencia_b.mp3` |
| Español (audiolibro) | `generar_audiolibro.py` | `Qwen3-TTS-12Hz-1.7B-Base-GGUF` | `audiolibros/<libro>/bloque_NN.mp3` |
| Inglés | `speak_clon_english.py` | `Qwen3-TTS-12Hz-1.7B-Base-GGUF` | `clon_narradora_english.mp3` |

## Audiolibro por bloques

`generar_audiolibro.py` toma los `bloque_*.txt` de
`epub_extract/preparado/<libro>/`, sintetiza cada bloque con
`narradora_consistencia_b`, fuerza `language: "es"` y guarda un MP3 por bloque.
Al final los une con `ffmpeg -c copy`, sin recodificar: los MP3 originales del
TTS quedan intactos.

```bash
cd /home/kritt/Git/script_videos/tts

# el único libro preparado
python generar_audiolibro.py

# elegir libro o pasar una ruta
python generar_audiolibro.py "NOMBRE_DEL_LIBRO"
python generar_audiolibro.py /ruta/a/preparado/libro

# regenerar bloques ya existentes / no unir
python generar_audiolibro.py --forzar
python generar_audiolibro.py --sin-unir
```

- Los bloques ya generados se omiten, así que se puede reanudar.
- Si el modelo TTS no está cargado, lo carga. Si falta VRAM porque Gemma sigue
  en memoria, descarga los otros modelos automáticamente y reintenta.
- Salida: `audiolibros/<libro>/bloque_01.mp3`, `bloque_02.mp3`, …,
  `audiolibro.mp3` y `manifiesto_audio.json`.
- El MP3 del TTS viene a 24 kHz, mono, 32 kbps. No se recompila.
- Cada bloque se divide en fragmentos de **250 palabras como máximo** y cada
  fragmento se pide por separado. Una sola petición con más de ~170 s de audio
  hace que el TTS degenere en repeticiones y ruido; con 250 palabras el
  fragmento queda en ~1.5–2 min. Los MP3 de los fragmentos se unen con
  `ffmpeg -c copy` y quedan en `partes/`.
- Ritmo medido por fragmento en el bloque técnico real: 140–176 ppm, promedio
  160 (1,679 palabras → 10.5 min). Por eso la preparación usa `--wpm 160`.
- Si el texto preparado cambió, regenera el audio con
  `python generar_audiolibro.py "NOMBRE_DEL_LIBRO" --forzar`.

Los dos scripts buscan la voz por nombre y transcripción. Si no existe, suben la
referencia, la registran y continúan. No hay IDs escritos a mano.

## Verificación de clonación

| Audio | F0 global | Inicio | Final |
|---|---:|---:|---:|
| `consistencia_b.mp3` | 163.3 Hz | — | — |
| `clon_consistencia_b.mp3` | 155.3 Hz | 163.3 Hz | 142.9 Hz |
| `referencia_narradora_english.wav` | 195.1 Hz | 195.1 Hz | 192.8 Hz |
| `clon_narradora_english_corto.mp3` | 190.5 Hz | 190.5 Hz | 190.5 Hz |
| `clon_narradora_english.mp3` | 190.5 Hz | 197.5 Hz | 190.5 Hz |

El timbre se mantiene con texto y emoción distintos, en ambos idiomas.

## Catálogo VoiceDesign

Se conservan como alternativas para diseñar voces nuevas. No son las voces de
producción.

Español (`speak_v.0.2.py`):

- `narrador_documental`
- `narrador_femenino`
- `asistente_cercano`
- `personaje_dramatico`

Inglés (`speak_english.py`):

- `documentary_narrator`
- `female_narrator`
- `warm_assistant`
- `dramatic_character`
- `narradora_english` — descripción que generó la referencia congelada

## Reglas

- El modelo **Base** solo clona. VoiceDesign y CustomVoice no.
- `reference_text` debe coincidir con el audio de referencia, no con el texto a
  generar. Si no coincide, la clonación deriva.
- `language` es el idioma del texto a generar: `es`, `en` o `auto`.
- `seed=12345` hace reproducible una voz, pero no iguala voces entre idiomas.
- El auto-switch de modelos está desactivado: hay que cargar el modelo antes de
  generar o la petición devuelve 400. Para cambiar de modelo, descargar el actual
  con `POST /api/inference/unload` y luego cargar el nuevo.
