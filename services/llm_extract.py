from openai import OpenAI

from config import settings

client = OpenAI(api_key=settings.OPENAI_API_KEY, timeout=30, max_retries=2)

SYSTEM = (
    "You are a product data assistant for a guitar store. Extract key specs in flat JSON; "
    "if unknown, omit."
)


def extract_attributes(name: str, hint: str | None = None) -> dict:
    try:
        completion = client.responses.create(
            model="gpt-5",
            input=[
                {"role": "system", "content": SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Product name: {name}\nHint: {hint or ''}\nReturn JSON only."
                    ),
                },
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        return getattr(completion, "output_parsed", {}) or {}
    except Exception as e:  # pragma: no cover - network call handling
        return {"_error": str(e)}
