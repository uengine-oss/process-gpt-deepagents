"""Runtime configuration for model routing (direct provider only).

This project intentionally does **not** use an OpenAI-compatible proxy layer.
Set `LLM_MODEL` (default: `anthropic:claude-opus-4-6`) and `ANTHROPIC_API_KEY`.
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
    route_mode: str  # direct
    openai_base_url: str | None


def resolve_model_routing(
    extras: dict[str, Any] | None,
    *,
    default_model: str = "anthropic:claude-opus-4-6",
    default_embedding_model: str | None = None,
    policy: str = "warn_fallback",
) -> ModelRoutingConfig:
    """Resolve model name (direct provider routing only)."""

    payload = extras or {}
    # Environment model is the primary source of truth; extras.model is fallback only.
    model_name = str(os.environ.get("LLM_MODEL") or payload.get("model") or default_model)
    embedding_model_env = (os.environ.get("LLM_EMBEDDING_MODEL") or "").strip()
    embedding_model = embedding_model_env or default_embedding_model

    # Normalize short model names to an explicit provider prefix.
    if ":" not in model_name:
        model_name = f"anthropic:{model_name}"

    proxy_url = (os.environ.get("LLM_PROXY_URL") or "").strip() or None
    if proxy_url:
        logger.warning(
            "Ignoring LLM_PROXY_URL=%r (proxy mode disabled; using direct provider).",
            proxy_url,
        )

    if policy != "warn_fallback":
        logger.warning("Unknown routing policy '%s'; using warn_fallback.", policy)
    logger.info("LLM routing resolved: mode=direct model=%s", model_name)
    return ModelRoutingConfig(
        model_name=model_name,
        embedding_model_name=embedding_model,
        route_mode="direct",
        openai_base_url=None,
    )

