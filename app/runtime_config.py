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


@dataclass(frozen=True)
class ModelRoutingConfig:
    """Resolved model + routing mode for the current execution."""

    model_name: str
    embedding_model_name: str | None
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
    default_embedding_model: str | None = None,
    policy: str = "warn_fallback",
) -> ModelRoutingConfig:
    """Resolve model name and whether to use an OpenAI-compatible LLM proxy (LiteLLM, etc.)."""

    payload = extras or {}
    # Environment model is the primary source of truth; extras.model is fallback only.
    model_name = str(os.environ.get("LLM_MODEL") or payload.get("model") or default_model)
    embedding_model_env = (os.environ.get("LLM_EMBEDDING_MODEL") or "").strip()
    embedding_model = embedding_model_env or default_embedding_model

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
            embedding_model_name=embedding_model,
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
        embedding_model_name=embedding_model,
        route_mode="direct",
        openai_base_url=None,
        proxy_api_key=None,
    )
