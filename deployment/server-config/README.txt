Server templates for the guide and voice (no secrets). `python -m scripts.demo create-env` copies them into
an external config folder next to the environment file; the container reads that folder at /config (read-only).
Keys stay in a separate external secrets folder mounted at /run/secrets/musiyo (read-only):
  groq.key, elevenlabs.key — one line each, never inside a repository.
guide.json: Groq answers with the extractive guide as fallback (same as the local session).
voice.json: Groq Whisper + ElevenLabs. The Windows voices do not exist on Linux; Piper needs the Linux
binary, which the image does not include yet, so without ElevenLabs credits the guide answers in text only.
