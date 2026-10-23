import os

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI


def _tracing_clients() -> dict:
    """상관키를 매 요청 헤더에 붙이는 httpx 클라이언트.

    LiteLLM 프록시 콜백이 이 헤더를 읽어, LLM 스팬을 MCP 스팬과 같은
    trace_id 로 묶는다. 실패하면 빈 dict — 계측 때문에 LLM 호출이
    막히면 안 된다.
    """
    try:
        from ..observability import tracing_http_clients

        return tracing_http_clients()
    except Exception:  # noqa: BLE001
        return {}


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
            **_tracing_clients(),
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
