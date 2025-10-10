"""LLM prompt used to infer structured product attributes from raw text."""

from openai import OpenAI

from config import settings

client = OpenAI(api_key=settings.OPENAI_API_KEY)


def extract_attributes(name, hint):
    """Generate a dictionary of product specs using the OpenAI Responses API."""
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
