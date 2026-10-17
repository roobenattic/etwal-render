# etwal-render

ffmpeg renderer for **Etwal Matinal Mizik** (lyric video 16:9 + Short 9:16).

- Triggered by n8n (workflow **EMM-04 Video**) through `workflow_dispatch` with a base64 JSON spec.
- The spec only contains short-lived signed URLs (download + upload). No secrets live in this repo.
- Output: MP4 uploaded straight to Supabase Storage, then a callback to n8n with the result.

Local test: `python3 render.py spec.json` (needs ffmpeg).
