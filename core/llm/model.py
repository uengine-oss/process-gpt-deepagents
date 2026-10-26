import os

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI


def _use_responses_api() -> bool | None:
    # 추론 모델은 chat/completions 에서 도구 호출을 거부하므로 /v1/responses 로 보낸다(opt-in).
    # 끄면 None 으로 둬 langchain-openai 기본 판단을 그대로 따른다.
    if os.getenv("LLM_USE_RESPONSES_API", "").strip().lower() in ("true", "1", "yes"):
        return True
    return None


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
            use_responses_api=_use_responses_api(),
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
            use_responses_api=_use_responses_api(),
        )

    raise ValueError(
        "No LLM credentials found. "
        "Set LLM_PROXY_URL+LLM_PROXY_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY."
    )
