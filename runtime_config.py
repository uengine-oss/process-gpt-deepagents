"""Runtime configuration for model routing and LiteLLM proxy policy."""

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
    route_mode: str  # proxy | direct-fallback
    openai_base_url: str | None


def resolve_model_routing(
    extras: dict[str, Any] | None,
    *,
    default_model: str = "gpt-4o",
    default_embedding_model: str = "text-embedding-3-small",
    policy: str = "warn_fallback",
) -> ModelRoutingConfig:
    """Resolve model and routing policy.

    policy=warn_fallback:
    - If LLM_PROXY_URL is set, treat calls as proxy-routed.
    - If not set, warn and allow direct provider route.
    """

    payload = extras or {}
    # Environment model is the primary source of truth; extras.model is fallback only.
    model_name = str(os.environ.get("LLM_MODEL") or payload.get("model") or default_model)
    embedding_model = str(os.environ.get("LLM_EMBEDDING_MODEL") or default_embedding_model)
    proxy_url = (os.environ.get("LLM_PROXY_URL") or "").strip() or None
    proxy_api_key = (os.environ.get("LLM_PROXY_API_KEY") or "").strip() or None

    # Normalize short model names to OpenAI provider prefix expected by deepagents/langchain.
    if ":" not in model_name:
        model_name = f"openai:{model_name}"

    if policy != "warn_fallback":
        logger.warning("Unknown routing policy '%s'; using warn_fallback.", policy)

    if proxy_url:
        # Export OpenAI-compatible envs for downstream SDK layers.
        os.environ["OPENAI_BASE_URL"] = proxy_url.rstrip("/")
        if proxy_api_key:
            os.environ["OPENAI_API_KEY"] = proxy_api_key
        logger.info(
            "LLM routing resolved: mode=proxy model=%s base_url=%s",
            model_name,
            proxy_url,
        )
        return ModelRoutingConfig(
            model_name=model_name,
            embedding_model_name=embedding_model,
            route_mode="proxy",
            openai_base_url=proxy_url,
        )

    logger.warning(
        "LLM proxy base URL is not configured (LLM_PROXY_URL missing). "
        "Continuing with direct provider fallback. model=%s",
        model_name,
    )
    return ModelRoutingConfig(
        model_name=model_name,
        embedding_model_name=embedding_model,
        route_mode="direct-fallback",
        openai_base_url=None,
    )

