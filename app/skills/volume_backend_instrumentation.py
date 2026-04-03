"""Wrap the routed skill FilesystemBackend so skill path IO emits tool_usage_* events."""

from __future__ import annotations

from typing import Any

from app.skills.volume_tool_events import log_skill_volume_tool


class InstrumentingSkillVolumeBackend:
    """Delegate to inner backend; log reads/lists/search under the skills volume route."""

    def __init__(self, inner: Any, virtual_route_prefix: str) -> None:
        self._inner = inner
        r = (virtual_route_prefix or "/").rstrip("/")
        self._route_prefix = r + "/"

    def _display_path(self, backend_path: str) -> str:
        p = (backend_path or "/").strip()
        if not p.startswith("/"):
            p = "/" + p
        rest = p.lstrip("/")
        base = self._route_prefix.rstrip("/")
        return f"{base}/{rest}" if rest else base

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def ls_info(self, path: str) -> list[Any]:
        disp = self._display_path(path)
        try:
            out = self._inner.ls_info(path)
            log_skill_volume_tool(
                tool_name="ls",
                tool_input={"path": disp},
                tool_output=[x.get("path") if isinstance(x, dict) else x for x in (out or [])],
                error=None,
            )
            return out
        except Exception as e:
            log_skill_volume_tool(
                tool_name="ls",
                tool_input={"path": disp},
                tool_output=None,
                error=str(e),
            )
            raise

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        disp = self._display_path(file_path)
        try:
            out = self._inner.read(file_path, offset=offset, limit=limit)
            log_skill_volume_tool(
                tool_name="read_file",
                tool_input={"file_path": disp, "offset": offset, "limit": limit},
                tool_output=out,
                error=None,
            )
            return out
        except Exception as e:
            log_skill_volume_tool(
                tool_name="read_file",
                tool_input={"file_path": disp, "offset": offset, "limit": limit},
                tool_output=None,
                error=str(e),
            )
            raise

    def glob_info(self, pattern: str, path: str = "/") -> list[Any]:
        disp = self._display_path(path)
        try:
            out = self._inner.glob_info(pattern, path)
            log_skill_volume_tool(
                tool_name="glob",
                tool_input={"pattern": pattern, "path": disp},
                tool_output=[x.get("path") if isinstance(x, dict) else x for x in (out or [])],
                error=None,
            )
            return out
        except Exception as e:
            log_skill_volume_tool(
                tool_name="glob",
                tool_input={"pattern": pattern, "path": disp},
                tool_output=None,
                error=str(e),
            )
            raise

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[Any] | str:
        disp = self._display_path(path) if path else self._route_prefix.rstrip("/")
        try:
            out = self._inner.grep_raw(pattern, path, glob)
            log_skill_volume_tool(
                tool_name="grep",
                tool_input={"pattern": pattern, "path": disp, "glob": glob},
                tool_output=out,
                error=None,
            )
            return out
        except Exception as e:
            log_skill_volume_tool(
                tool_name="grep",
                tool_input={"pattern": pattern, "path": disp, "glob": glob},
                tool_output=None,
                error=str(e),
            )
            raise
