"""
Single LLM entry point. This is the ONLY file that imports openai.
Every other module uses: from src.llm_client import chat_completion
"""
import logging
from openai import OpenAI
from src.config import settings

logger = logging.getLogger(__name__)

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=settings.openai_api_key)
    return _client


def chat_completion(
    messages: list[dict],
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
) -> str:
    """Single entry point for all LLM calls. No other file imports openai."""
    client = get_client()
    logger.info("LLM call using model: %s", settings.llm_model)
    kwargs: dict = dict(
        model=settings.llm_model,
        messages=messages,
        temperature=temperature if temperature is not None else settings.llm_temperature,
        max_tokens=max_tokens if max_tokens is not None else settings.llm_max_tokens,
    )
    if response_format is not None:
        kwargs["response_format"] = response_format
    response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content


def chat_completion_json(messages: list[dict], **kwargs) -> str:
    """LLM call that forces JSON output."""
    return chat_completion(
        messages=messages,
        response_format={"type": "json_object"},
        **kwargs,
    )
