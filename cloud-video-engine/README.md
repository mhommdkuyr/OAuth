# Cloud Video Engine

API for code-driven Remotion motion graphics and basic FFmpeg video editing.

## Endpoints
- `GET /health`
- `POST /api/v1/render` — authenticated synchronous render request.

## Deploy
1. Create a public Supabase Storage bucket named `video-renders` (or configure `SUPABASE_STORAGE_BUCKET`).
2. In Render, create a Docker Web Service from this repository and set **Root Directory** to `cloud-video-engine`.
3. Add all values from `.env.example` as Render environment variables. Use a strong `API_KEY`.
4. Deploy and replace the placeholder URL in `openapi.json` with the Render service URL.
5. Import `openapi.json` into your Custom GPT Action and set Bearer authentication to the same API key.

Never commit `.env` or expose the Supabase service-role key. Public bucket URLs can be accessed by anyone who has the link. Rendering is resource-intensive; test with short videos before production use. This starter uses synchronous requests, not a durable background queue. Review Remotion's license for commercial use.
