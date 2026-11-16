"""Settings (loaded from env / .env) and swappable LLM / embedding provider factories."""

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class ProviderError(RuntimeError):
    """LLM/embedding provider is misconfigured (missing key, unknown provider)."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Provider
    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    llm_model: str = "gemini-2.5-flash"
    embedding_model: str = "models/gemini-embedding-001"

    # Elasticsearch (falls back to in-memory FAISS when unreachable)
    elasticsearch_url: str = "http://localhost:9200"
    es_index: str = "opsmind_runbooks"
    # ES basic license has no RRF, so hybrid = BM25 score + boosted kNN score.
    es_knn_boost: float = 20.0
    es_bm25_boost: float = 1.0
    es_service_boost: float = 3.0

    # Retrieval
    runbooks_dir: str = str(ROOT_DIR / "runbooks")
    top_k: int = 3
    chunk_max_chars: int = 1500


@lru_cache
def get_settings() -> Settings:
    return Settings()


def build_chat_model(settings: Settings) -> Any:
    """Return a LangChain chat model. Add new providers here only."""
    if settings.llm_provider == "gemini":
        if not settings.gemini_api_key:
            raise ProviderError("GEMINI_API_KEY is not set")
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=settings.llm_model,
            google_api_key=settings.gemini_api_key,
            temperature=0,
        )
    raise ProviderError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")


def build_embeddings(settings: Settings) -> Any:
    """Return a LangChain Embeddings object. Add new providers here only."""
    if settings.llm_provider == "gemini":
        if not settings.gemini_api_key:
            raise ProviderError("GEMINI_API_KEY is not set")
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        return GoogleGenerativeAIEmbeddings(
            model=settings.embedding_model,
            google_api_key=settings.gemini_api_key,
        )
    raise ProviderError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")
