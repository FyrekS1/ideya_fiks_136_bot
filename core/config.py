import os
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

MODEL = os.getenv("MODEL", "zai-org/GLM-5.3-Flash")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "zai-org/GLM-5.3")
BASE_URL = os.getenv("BASE_URL", "https://api.reformboss.com/v1").rstrip("/")


def build_client():
    api_key = os.getenv("API_KEY")
    if not api_key:
        raise RuntimeError("Заполните API_KEY в .env")
    return AsyncOpenAI(
        api_key=api_key,
        base_url=BASE_URL,
        timeout=120,
        max_retries=2,
    )


if __name__ == "__main__":
    print(f"Primary model: {MODEL}")
    print(f"Fallback model: {FALLBACK_MODEL}")
    print(f"Base URL: {BASE_URL}")
    print("Ключ задан" if os.getenv("API_KEY") else "Ключ не задан")
