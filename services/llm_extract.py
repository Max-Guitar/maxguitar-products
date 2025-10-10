from openai import OpenAI

from config import settings

client = OpenAI(api_key=settings.OPENAI_API_KEY, timeout=30, max_retries=2)


def extract_attributes(name, hint):
    prompt = f"""
    You are a product data assistant for a guitar store.
    Given the following info, extract key specs (bridge type, strings, color, pickups, handedness, etc.) in JSON.
    Product name: {name}
    Hint: {hint}
    Return JSON only.
    """
    completion = client.responses.create(
        model="gpt-5",
        input=prompt,
        temperature=0.2,
        response_format={"type": "json_object"}
    )
    return completion.output_parsed or {}
