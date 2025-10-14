"""LLM prompt used to infer structured product attributes from raw text."""

import json
import os
from typing import Any, Dict

from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
client = OpenAI(api_key=settings.OPENAI_API_KEY)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def extract_attributes(name: str, hint: str = "") -> Dict[str, Any]:
    """Extract normalized attributes using the OpenAI chat completions API."""
    system = (
        "You are a product data assistant for a guitar store. "
        "Extract only factual specs present or strongly implied by the name/hint. "
        "Return a compact JSON object with keys like bridge_type, strings, color, pickups, "
        "handedness, etc. If unknown, omit the key."
    )
    user = f"Product name: {name}\nHint: {hint or '(none)'}\nReturn JSON only."
    resp = client.chat.completions.create(
        model=MODEL,
        temperature=0.2,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    content = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(content)
        return data if isinstance(data, dict) else {}
    except Exception:
        snippet = content.strip()
        start, end = snippet.find("{"), snippet.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(snippet[start : end + 1])
            except Exception:
                pass
        return {}
