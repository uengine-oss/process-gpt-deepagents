import logging
import os
from pathlib import Path

from deepagents.middleware.skills import SkillsMiddleware, SkillsStateUpdate

_BASE_DIR = Path(__file__).parent.parent.parent

logger = logging.getLogger(__name__)


def _get_skills(tenant_id: str = "") -> list[str] | None:
    """Return SkillsMiddleware source paths.

    Each returned path must be a directory whose immediate subdirectories are
    individual skills (each containing SKILL.md). This matches how SkillsMiddleware
    scans sources with a single non-recursive ls call.

    Layout expected on disk:
      {root}/
        {collection}/          ← returned as a source (e.g. anthropics-skills, localhost)
          {skill-name}/
            SKILL.md
        {tenant_id}/           ← returned first when tenant_id is given
          {skill-name}/
            SKILL.md

    GitHub-fetched skills are written to {root}/{slug}/{skill-name}/SKILL.md by
    github_skill_fetcher.prefetch_github_skills(), so {root}/{slug}/ is discovered
    automatically here.
    """
    raw = os.getenv("SKILLS_DIRS", str(_BASE_DIR / "skills"))
    seen: set[str] = set()
    paths: list[str] = []

    def _add(p: str) -> None:
        if p not in seen:
            seen.add(p)
            paths.append(p)

    for p in raw.split(","):
        p = p.strip()
        if not p:
            continue
        root = str((_BASE_DIR / p).resolve()) if not os.path.isabs(p) else p.rstrip("/")

        # Tenant-specific collection first (skills uploaded for this tenant)
        if tenant_id:
            tenant_path = f"{root}/{tenant_id}"
            if Path(tenant_path).is_dir():
                _add(tenant_path)

        # Discover all collection subdirectories under root.
        # This picks up GitHub slugs (anthropics-skills/, etc.) and localhost/.
        # Tenant dir is excluded to avoid double-adding it.
        try:
            for entry in sorted(Path(root).iterdir()):
                if entry.is_dir() and entry.name != tenant_id:
                    _add(str(entry))
        except OSError:
            pass

    return paths if paths else None


class FilteredSkillsMiddleware(SkillsMiddleware):
    """SkillsMiddleware that restricts visible skills to a specified set.

    Loads skills from the full collection but filters state so only the
    allowed skill names appear in the system prompt. Skills not in
    `allowed_skills` are invisible to the sub-agent.
    """

    def __init__(self, *, backend, sources, allowed_skills: list[str], **kwargs):
        super().__init__(backend=backend, sources=sources, **kwargs)
        self._allowed: frozenset[str] = frozenset(allowed_skills)

    def _filter(self, update: SkillsStateUpdate | None) -> SkillsStateUpdate | None:
        if update is None:
            return None
        update["skills_metadata"] = [
            s for s in update.get("skills_metadata", [])
            if s["name"] in self._allowed
        ]
        return update

    def before_agent(self, state, runtime, config):
        result = self._filter(super().before_agent(state, runtime, config))
        names = [s["name"] for s in (result or {}).get("skills_metadata", [])]
        logger.info("FilteredSkillsMiddleware: allowed=%s loaded=%s", list(self._allowed), names)
        return result

    async def abefore_agent(self, state, runtime, config):
        result = self._filter(await super().abefore_agent(state, runtime, config))
        names = [s["name"] for s in (result or {}).get("skills_metadata", [])]
        logger.info("FilteredSkillsMiddleware: allowed=%s loaded=%s", list(self._allowed), names)
        return result
