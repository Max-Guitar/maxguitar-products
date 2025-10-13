"""LLM prompt used to infer structured product attributes from raw text."""
from typing import Optional, Dict, Any
from openai import OpenAI
from config import settings

client = OpenAI(api_key=settings.OPENAI_API_KEY, timeout=30, max_retries=2)

SYSTEM = (
    "You are a product data assistant for a guitar store. "
    "Extract key specs (bridge_type, strings, color, pickups, handedness, etc.) in flat JSON; "
    "omit unknown fields."
)

def extract_attributes(name: str, hint: Optional[str] = None) -> Dict[str, Any]:
    prompt = (
        f"Product name: {name}\n"
        f"Hint: {hint or ''}\n"
        "Return JSON only with simple string/number values."
    )
    try:
        completion = client.responses.create(
            model="gpt-5",
            input=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        return getattr(completion, "output_parsed", {}) or {}
    except Exception as e:
        return {"_error": str(e)}
