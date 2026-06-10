"""DeepAgents storage backend: ephemeral agent state + mounted skills on disk."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend

from app.skills.volume_backend_instrumentation import InstrumentingSkillVolumeBackend

if TYPE_CHECKING:
    from deepagents.backends.protocol import BackendFactory, BackendProtocol

def skills_route_prefix(skills_root_dir: str | None = None) -> tuple[str, str]:
    """Return (absolute route prefix ending with '/', resolved root without trailing slash)."""
    root = (skills_root_dir or os.environ.get("SKILLS_ROOT_DIR") or "/app/skills").rstrip("/")
    route = root if root.endswith("/") else root + "/"
    return route, root


def tenant_skill_sources_for_middleware(tenant_id: str | None) -> list[str] | None:
    """Paths for create_deep_agent(skills=...) — tenant volume slice + GitHub-fetched skills.

    GitHub skills are pre-fetched to disk under SKILLS_ROOT_DIR/{repo-slug}/ at invoke time,
    so the root route always includes them.
    """
    route, _ = skills_route_prefix(skills_root_dir=None)
    tid = (tenant_id or "").strip().strip("/")
    sources: list[str] = []
    if tid:
        sources.append(f"{route}{tid}/")
    sources.append(route)
    return sources


def tenant_memory_sources_for_middleware(tenant_id: str | None) -> list[str] | None:
    """Paths for create_deep_agent(memory=...) — optional AGENTS.md under tenant skills dir + env extras.

    Missing files are ignored by deepagents ``MemoryMiddleware`` (file_not_found).
    """
    seen: set[str] = set()
    out: list[str] = []
    extra = (os.environ.get("DEEP_AGENT_MEMORY_PATHS") or "").strip()
    if extra:
        for p in extra.split(","):
            s = p.strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    tid = (tenant_id or "").strip().strip("/")
    if tid:
        route, _ = skills_route_prefix(skills_root_dir=None)
        tenant_agents = f"{route}{tid}/AGENTS.md"
        if tenant_agents not in seen:
            seen.add(tenant_agents)
            out.append(tenant_agents)
    return out or None


def make_deepagent_backend_factory(
    skills_root_dir: str | None = None,
) -> "BackendFactory":
    """Factory for DeepAgents: StateBackend by default, real FS under mounted skills root.

    Exposes one FilesystemBackend route for skills on the Docker volume (both tenant-uploaded
    and GitHub-fetched skills written to SKILLS_ROOT_DIR at invoke time).

    Without this, the default StateBackend keeps all paths in LangGraph state only,
    so ``ls``/``read_file`` on ``<SKILLS_ROOT_DIR>/`` never sees Docker volume mounts.
    """
    route, root = skills_route_prefix(skills_root_dir)

    def _factory(rt) -> "BackendProtocol":
        fs = FilesystemBackend(root_dir=root, virtual_mode=False)
        fs_instrumented = InstrumentingSkillVolumeBackend(fs, virtual_route_prefix=route)
        return CompositeBackend(default=StateBackend(rt), routes={route: fs_instrumented})

    return _factory
