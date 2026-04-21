"""Runtime configuration for model routing (direct Anthropic or OpenAI-compatible proxy e.g. LiteLLM).

Direct: ``LLM_MODEL`` (default ``anthropic:claude-opus-4-6``) + ``ANTHROPIC_API_KEY``.

Proxy: set ``LLM_PROXY_URL`` to the **exact** OpenAI-compatible base URL your gateway documents
(no automatic ``/v1`` normalization) and set ``LLM_PROXY_API_KEY`` or ``OPENAI_API_KEY``.
The client uses ``langchain_openai.ChatOpenAI`` with ``base_url``; ``LLM_MODEL`` should match the
model id your proxy expects (often the substring after ``anthropic:``, or a full route name).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# LiteLLM UI / DB often stores ``provider/model``; LangChain ``init_chat_model`` expects ``provider:model``.
# Only single-slash ``left/right`` forms are rewritten; multi-segment ids (e.g. Fireworks paths) are left as-is.
_LITELLM_SLASH_PREFIX_TO_LANGCHAIN: dict[str, str] = {
    "anthropic": "anthropic",
    "azure": "azure_openai",
    "bedrock": "bedrock",
    "cohere": "cohere",
    "deepseek": "deepseek",
    "fireworks": "fireworks",
    "gemini": "google_genai",
    "google": "google_genai",
    "groq": "groq",
    "huggingface": "huggingface",
    "litellm": "litellm",
    "mistral": "mistralai",
    "mistralai": "mistralai",
    "nvidia": "nvidia",
    "ollama": "ollama",
    "openai": "openai",
    "openrouter": "openrouter",
    "perplexity": "perplexity",
    "together": "together",
    "vertex_ai": "google_vertexai",
    "xai": "xai",
}


def normalize_litellm_slash_model_name(model: str | None) -> str | None:
    """Map LiteLLM-style ``openai/gpt-4.1`` to LangChain-style ``openai:gpt-4.1`` when unambiguous.

    Strings that already contain ``:``, or that have zero or more than one ``/``, are returned unchanged.
    Unknown ``left`` segments (first path component) are also left unchanged.
    """
    if model is None:
        return None
    s = str(model).strip()
    if not s or ":" in s or "/" not in s:
        return s
    if s.count("/") != 1:
        return s
    left, right = s.split("/", 1)
    left_key = left.strip().lower().replace("-", "_")
    right = right.strip()
    if not left_key or not right:
        return s
    lc_provider = _LITELLM_SLASH_PREFIX_TO_LANGCHAIN.get(left_key)
    if not lc_provider:
        return s
    return f"{lc_provider}:{right}"


@dataclass(frozen=True)
class ModelRoutingConfig:
    """Resolved model + routing mode for the current execution."""

    model_name: str
    route_mode: str  # "direct" | "proxy"
    openai_base_url: str | None
    proxy_api_key: str | None


def _proxy_api_key_from_env() -> str | None:
    raw = (os.environ.get("LLM_PROXY_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    return raw or None


def resolve_model_routing(
    extras: dict[str, Any] | None,
    *,
    default_model: str = "anthropic:claude-opus-4-6",
    policy: str = "warn_fallback",
) -> ModelRoutingConfig:
    """Resolve model name and whether to use an OpenAI-compatible LLM proxy (LiteLLM, etc.)."""

    payload = extras or {}
    # Environment model is the primary source of truth; extras.model is fallback only.
    model_name = str(os.environ.get("LLM_MODEL") or payload.get("model") or default_model)
    model_name = normalize_litellm_slash_model_name(model_name.strip()) or model_name.strip()

    # Normalize short model names to an explicit provider prefix.
    if ":" not in model_name:
        model_name = f"anthropic:{model_name}"

    proxy_url = (os.environ.get("LLM_PROXY_URL") or "").strip() or None
    proxy_key = _proxy_api_key_from_env()

    if proxy_url and proxy_key:
        logger.info(
            "LLM routing resolved: mode=proxy base_url=%s model=%s",
            proxy_url.rstrip("/"),
            model_name,
        )
        return ModelRoutingConfig(
            model_name=model_name,
            route_mode="proxy",
            openai_base_url=proxy_url.rstrip("/"),
            proxy_api_key=proxy_key,
        )

    if proxy_url and not proxy_key:
        logger.warning(
            "LLM_PROXY_URL is set but neither LLM_PROXY_API_KEY nor OPENAI_API_KEY is set; "
            "falling back to direct Anthropic (set a proxy API key to use the gateway).",
        )

    if policy != "warn_fallback":
        logger.warning("Unknown routing policy '%s'; using warn_fallback.", policy)
    logger.info("LLM routing resolved: mode=direct model=%s", model_name)
    return ModelRoutingConfig(
        model_name=model_name,
        route_mode="direct",
        openai_base_url=None,
        proxy_api_key=None,
    )
