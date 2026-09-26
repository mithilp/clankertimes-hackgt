"""Send one tiny request to each model to see which ones this key can actually use (free tier or not).

    docker compose run --rm --entrypoint python editor scripts/probe_models.py gemini-3.8-flash gemini-3.5-flash-lite
"""

import os
import sys

from google import genai
from google.genai import errors

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
for model in sys.argv[1:]:
    try:
        r = client.models.generate_content(model=model, contents="Reply with exactly: OK")
        print(f"{model:30} OK   -> {(r.text or '').strip()[:20]!r}")
    except errors.APIError as e:
        print(f"{model:30} FAIL -> {e.code} {e.status}: {(e.message or '')[:120]}")
