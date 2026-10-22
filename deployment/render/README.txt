RENDER (API) — archivos y valores

Variables que pide el Blueprint (render.yaml), sync: false:
  MUSIYO_DATABASE_URL          Cadena de Supabase (Session pooler), empezando por postgresql+psycopg://
  MUSIYO_WEB_ORIGIN            Dirección de la Web en Vercel, p. ej. https://musiyo.vercel.app (sin / final)
  MUSIYO_CONTENT_ARCHIVE_URL   Enlaces firmados de las partes del paquete en Supabase Storage (https://…), en orden
                               (.part1 primero), separados por un espacio. Supabase gratis limita cada archivo a 50 MB.
  MUSIYO_CONTENT_ARCHIVE_SHA256  Huella que imprimió scripts.package_content

Secret Files (Render → Environment → Secret Files), se montan en /etc/secrets:
  guide.json                 Copia de deployment/render/guide.json (sin llaves dentro)
  voice.json                 Copia de deployment/render/voice.json (sin llaves dentro)
  quiz.json                  La encuesta publicada (coordination/quiz-published.json)
  groq.key                   Solo la llave de Groq, una línea
  elevenlabs.key             Solo la llave de ElevenLabs, una línea
  validator-invitation.txt   El código de invitación para validadores (12+ caracteres); sin él no hay registro

Plan gratuito: el servicio se duerme tras 15 minutos sin visitas (la primera visita tarda ~1 minuto) y su disco
se borra al reiniciar; por eso al arrancar descarga y verifica el paquete de contenido.
