"""Wrap non-LangChain tools as LangChain StructuredTool."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from langchain_core.tools import BaseTool as LangChainBaseTool
    from langchain_core.tools import StructuredTool as LangChainStructuredTool
    from pydantic import BaseModel as PydanticBaseModel

    _LANGCHAIN_TOOLS_AVAILABLE = True
except ImportError:
    LangChainBaseTool = None  # type: ignore[misc, assignment]
    LangChainStructuredTool = None  # type: ignore[misc, assignment]
    PydanticBaseModel = None  # type: ignore[misc, assignment]
    _LANGCHAIN_TOOLS_AVAILABLE = False


def _ensure_langchain_tools(tools: list[Any]) -> list[Any]:
    """CrewAI/MCP 툴을 LangChain BaseTool로 래핑하여 tool decorator __name__ 오류를 방지합니다."""
    if not _LANGCHAIN_TOOLS_AVAILABLE or not tools:
        return tools

    result: list[Any] = []
    for t in tools:
        if isinstance(t, LangChainBaseTool):
            result.append(t)
            continue

        name = getattr(t, "name", None) or getattr(t, "__class__", type(t)).__name__
        description = getattr(t, "description", None) or str(t)

        def _make_sync_wrapper(original: Any) -> Any:
            def _run(**kwargs: Any) -> Any:
                if hasattr(original, "run"):
                    return original.run(**kwargs)
                if hasattr(original, "invoke"):
                    return original.invoke(kwargs)
                raise RuntimeError(f"Tool {getattr(original, 'name', original)} has no run/invoke")

            return _run

        def _make_async_wrapper(original: Any) -> Any:
            async def _arun(**kwargs: Any) -> Any:
                if hasattr(original, "arun"):
                    return await original.arun(**kwargs)
                if hasattr(original, "ainvoke"):
                    return await original.ainvoke(kwargs)
                sync_run = _make_sync_wrapper(original)
                return sync_run(**kwargs)

            return _arun

        sync_func = _make_sync_wrapper(t)
        async_func = _make_async_wrapper(t)
        safe_name = (name or "unknown").replace(" ", "_")
        sync_func.__name__ = safe_name  # type: ignore[attr-defined]
        async_func.__name__ = safe_name  # type: ignore[attr-defined]

        args_schema = getattr(t, "args_schema", None)
        use_schema = (
            args_schema is not None
            and isinstance(args_schema, type)
            and PydanticBaseModel is not None
            and issubclass(args_schema, PydanticBaseModel)
        )

        try:
            if use_schema:
                wrapped = LangChainStructuredTool(
                    name=name,
                    description=description or name,
                    func=sync_func,
                    coroutine=async_func,
                    args_schema=args_schema,
                )
            else:
                _DynamicSchema = type(
                    f"_Schema_{safe_name}",
                    (PydanticBaseModel,),
                    {"model_config": {"extra": "allow"}, "__annotations__": {}},
                )
                wrapped = LangChainStructuredTool(
                    name=name,
                    description=description or name,
                    func=sync_func,
                    coroutine=async_func,
                    args_schema=_DynamicSchema,
                )
        except Exception as e:
            logger.warning(
                "CrewAI/MCP tool wrap failed, using minimal schema: %s | tool=%s",
                e,
                name,
            )
            _FallbackSchema = type(
                f"_Fallback_{safe_name}",
                (PydanticBaseModel,),
                {"model_config": {"extra": "allow"}, "__annotations__": {}},
            )
            wrapped = LangChainStructuredTool(
                name=name,
                description=description or name,
                func=sync_func,
                coroutine=async_func,
                args_schema=_FallbackSchema,
            )

        if hasattr(t, "_mcp_server"):
            setattr(wrapped, "_mcp_server", getattr(t, "_mcp_server"))
        result.append(wrapped)

    return result
