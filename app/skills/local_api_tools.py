"""Local skills-files HTTP API tools for subagents.

These tools provide a fallback path for reading tenant-local skill files when
`claude-skills` MCP tools (e.g. read_skill_document / find_helpful_skills) do not
surface tenant-local skills.

API contract (as implemented by our local service):
- GET  /skills/<skill_name>/files
  -> returns a JSON document describing available files (shape is service-defined)
- GET  /skills/<skill_name>/files/<path...>
  -> returns file content (typically text)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

try:
    from langchain_core.tools import StructuredTool as LangChainStructuredTool
    from pydantic import BaseModel, Field

    _TOOLS_AVAILABLE = True
except Exception:  # pragma: no cover
    LangChainStructuredTool = None  # type: ignore[assignment]
    BaseModel = None  # type: ignore[assignment]
    Field = None  # type: ignore[assignment]
    _TOOLS_AVAILABLE = False


def _skills_api_base_url() -> str:
    """Return base URL for local skills API (no trailing slash)."""
    return (os.environ.get("PROCESSGPT_SKILLS_API_BASE_URL") or "http://localhost:8765").rstrip(
        "/"
    )


def _http_get_text(url: str, timeout_s: float = 20.0) -> tuple[str, str | None]:
    """Fetch URL and return (text, content_type)."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            content_type = resp.headers.get("Content-Type")
            raw = resp.read()
            # Best-effort decode. If server sends charset, honor it.
            encoding = "utf-8"
            if content_type and "charset=" in content_type:
                encoding = content_type.split("charset=", 1)[1].split(";", 1)[0].strip() or "utf-8"
            try:
                text = raw.decode(encoding, errors="replace")
            except Exception:
                text = raw.decode("utf-8", errors="replace")
            return text, content_type
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {e.code} for {url}: {body}".strip()) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to reach {url}: {e}") from e


def _build_list_files_url(skill_name: str) -> str:
    base = _skills_api_base_url()
    return f"{base}/skills/{quote(skill_name, safe='')}/files"


def _build_read_file_url(skill_name: str, file_path: str) -> str:
    base = _skills_api_base_url()
    # Preserve slashes in file_path while encoding unsafe characters.
    encoded_path = quote(file_path.lstrip("/"), safe="/")
    return f"{base}/skills/{quote(skill_name, safe='')}/files/{encoded_path}"


@dataclass(frozen=True)
class _ToolSpec:
    name: str
    description: str


def build_local_skills_api_tools(*, mcp_server_name: str = "claude-skills") -> list[Any]:
    """Return LangChain tools for local skills HTTP API.

    The tools are tagged with _mcp_server so that existing wiring which always
    attaches the default MCP server toolsets can include these tools too.
    """
    if not _TOOLS_AVAILABLE:
        return []

    class ListSkillFilesArgs(BaseModel):
        skill_name: str = Field(..., description="Skill name to list files for.")

    def list_skill_files(*, skill_name: str) -> dict[str, Any]:
        """List available files for a tenant-local skill."""
        url = _build_list_files_url(skill_name)
        text, content_type = _http_get_text(url)
        # If JSON, parse; else return raw text.
        try:
            parsed = json.loads(text)
            return {"skill_name": skill_name, "url": url, "content_type": content_type, "data": parsed}
        except Exception:
            return {"skill_name": skill_name, "url": url, "content_type": content_type, "text": text}

    class ReadSkillFileArgs(BaseModel):
        skill_name: str = Field(..., description="Skill name to read file from.")
        file_path: str = Field(
            ...,
            description=(
                "File path relative to the skill root (e.g. 'SKILL.md' or 'scripts/foo.py'). "
                "Do not include '/skills/<skill_name>/files' prefix."
            ),
        )

    def read_skill_file(*, skill_name: str, file_path: str) -> dict[str, Any]:
        """Read a specific file inside a tenant-local skill."""
        url = _build_read_file_url(skill_name, file_path)
        text, content_type = _http_get_text(url)
        return {
            "skill_name": skill_name,
            "file_path": file_path,
            "url": url,
            "content_type": content_type,
            "text": text,
        }

    tool_specs = [
        _ToolSpec(
            name="list_skill_files_http",
            description=(
                "List files inside a tenant-local skill via HTTP API at "
                f"{_skills_api_base_url()}/skills/<skill_name>/files"
            ),
        ),
        _ToolSpec(
            name="read_skill_file_http",
            description=(
                "Read a file inside a tenant-local skill via HTTP API at "
                f"{_skills_api_base_url()}/skills/<skill_name>/files/<path>"
            ),
        ),
    ]

    list_tool = LangChainStructuredTool(
        name=tool_specs[0].name,
        description=tool_specs[0].description,
        func=list_skill_files,
        args_schema=ListSkillFilesArgs,
    )
    read_tool = LangChainStructuredTool(
        name=tool_specs[1].name,
        description=tool_specs[1].description,
        func=read_skill_file,
        args_schema=ReadSkillFileArgs,
    )

    # Tag as part of the default MCP server toolset so subagents receive them automatically.
    setattr(list_tool, "_mcp_server", mcp_server_name)
    setattr(read_tool, "_mcp_server", mcp_server_name)

    return [list_tool, read_tool]

