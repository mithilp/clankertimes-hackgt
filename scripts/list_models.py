"""List the Gemini models this GEMINI_API_KEY can call. Never prints the key.

    docker compose run --rm --entrypoint python editor scripts/list_models.py
"""

import os

from google import genai

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
for m in client.models.list():
    actions = getattr(m, "supported_actions", None) or []
    if "generateContent" in actions:
        print(f"{m.name.removeprefix('models/'):45} in={m.input_token_limit} out={m.output_token_limit}")
