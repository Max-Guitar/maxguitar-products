import os

try:
    import streamlit as st
    SECRETS = st.secrets
except Exception:
    SECRETS = {}

def _get(name: str) -> str:
    return os.getenv(name) or SECRETS.get(name, "")


class Settings:
    MAGENTO_BASE_URL: str = _get("MAGENTO_BASE_URL")
    MAGENTO_ADMIN_TOKEN: str = _get("MAGENTO_ADMIN_TOKEN")
    OPENAI_API_KEY: str = _get("OPENAI_API_KEY")


settings = Settings()

# optional: fail fast in Streamlit with a clear message
if not all([
    settings.MAGENTO_BASE_URL,
    settings.MAGENTO_ADMIN_TOKEN,
    settings.OPENAI_API_KEY,
]):
    raise RuntimeError(
        "Set MAGENTO_BASE_URL, MAGENTO_ADMIN_TOKEN, OPENAI_API_KEY in Streamlit Secrets."
    )
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    MAGENTO_BASE_URL: str
    MAGENTO_ADMIN_TOKEN: str
    OPENAI_API_KEY: str

    class Config:
        env_file = ".env"

settings = Settings()
