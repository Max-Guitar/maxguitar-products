from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    MAGENTO_BASE_URL: str
    MAGENTO_ADMIN_TOKEN: str
    OPENAI_API_KEY: str

    class Config:
        env_file = ".env"

settings = Settings()
