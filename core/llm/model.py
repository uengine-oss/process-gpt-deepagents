import os

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI


def _build_model():
    proxy_url = os.getenv("LLM_PROXY_URL")
    proxy_key = os.getenv("LLM_PROXY_API_KEY")
    llm_model = os.getenv("LLM_MODEL")

    if proxy_url and proxy_key:
        return ChatOpenAI(
            model=llm_model or "gpt-4o",
            base_url=proxy_url,
            api_key=proxy_key,
            timeout=180,
            max_retries=2,
        )

    if os.getenv("ANTHROPIC_API_KEY"):
        return ChatAnthropic(
            model_name=llm_model or "claude-sonnet-4-6",
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            timeout=180,
            max_retries=2,
        )

    if os.getenv("OPENAI_API_KEY"):
        return ChatOpenAI(
            model=llm_model or "gpt-4o",
            api_key=os.getenv("OPENAI_API_KEY"),
            timeout=180,
            max_retries=2,
        )

    raise ValueError(
        "No LLM credentials found. "
        "Set LLM_PROXY_URL+LLM_PROXY_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY."
    )
