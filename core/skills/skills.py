import logging
import os
import re
from pathlib import Path

import yaml
from deepagents.middleware.skills import SkillsMiddleware, SkillsStateUpdate

_BASE_DIR = Path(__file__).parent.parent.parent

logger = logging.getLogger(__name__)


def _global_collection_names() -> set[str]:
    """Return directory names under the skills root that are legitimate
    global collections, keyed by repo slug (git-fetched) plus the bundled
    local system-skills collection.

    This is a whitelist, not a blacklist: only directories produced by
    `git_skill_fetcher.prefetch_git_skills` or `local_system_skills.seed_
    local_system_skills` are considered global. Anything else directly
    under the skills root (most notably another tenant's `{tenant_id}/`
    directory) must never be treated as a global collection, or one
    tenant's agent would see — and be able to read_file — another
    tenant's private skills.

    `local_system_skills.SYSTEM_COLLECTION_NAME` is included unconditionally
    (independent of `SKILL_REPO_URLS`/git config) so process-gpt's own
    bundled system skills stay available with zero network dependency —
    required for closed/on-prem installs where outbound git access may not
    exist.
    """
    from .git_skill_fetcher import _configured_repos, _parse_repo_url, _repo_slug
    from .local_system_skills import SYSTEM_COLLECTION_NAME

    names: set[str] = {SYSTEM_COLLECTION_NAME}
    for url in _configured_repos():
        try:
            owner, repo = _parse_repo_url(url)
        except ValueError:
            continue
        names.add(_repo_slug(owner, repo))
    return names


def _get_skills(tenant_id: str = "", provider_config_id: str = "") -> list[str] | None:
    """Return SkillsMiddleware source paths.

    Each returned path must be a directory whose immediate subdirectories are
    individual skills (each containing SKILL.md). This matches how SkillsMiddleware
    scans sources with a single non-recursive ls call.

    Layout on disk (provider-keyed):
      {root}/
        {collection}/                    ← global collections (e.g. anthropics-skills)
          {skill-name}/SKILL.md
        {tenant_id}/
          {provider_config_id}/          ← provider-keyed skills (tenant_git_config.id)
            {skill-name}/SKILL.md
          local/                         ← skills uploaded via ZIP or external git URL
            {skill-name}/SKILL.md

    The flat {tenant_id}/ path is always included so that non-repo skills
    (directories directly under the tenant dir) remain discoverable.
    When provider_config_id is given the function additionally returns the
    keyed path and the local-upload path.
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

        if tenant_id:
            if provider_config_id:
                # Provider-keyed: skills linked to this specific git config
                keyed = f"{root}/{tenant_id}/{provider_config_id}"
                if Path(keyed).is_dir():
                    _add(keyed)
            # Locally uploaded skills always included
            local = f"{root}/{tenant_id}/local"
            if Path(local).is_dir():
                _add(local)
            # Flat {tenant_id}/ structure — always included so non-repo skills
            # (directories directly under the tenant dir) are discoverable.
            tenant_path = f"{root}/{tenant_id}"
            if Path(tenant_path).is_dir():
                _add(tenant_path)

        # Global collections (anthropics-skills, etc.) — whitelist by known
        # repo slug so other tenants' directories are never picked up here.
        global_names = _global_collection_names()
        try:
            for entry in sorted(Path(root).iterdir()):
                if entry.is_dir() and entry.name in global_names:
                    _add(str(entry))
        except OSError:
            pass

    return paths if paths else None


def resolve_slash_skill(tenant_id: str, requested_name: str) -> str:
    """슬래시 커맨드(`/skill-name`)로 지정된 스킬이 이 테넌트에서 실제로 사용 가능하면
    이름을 그대로, 아니면 빈 문자열을 반환한다.

    `/skills` API가 프론트에 노출하는 목록과 동일한 기준(`_scan_skills`)을 사용해,
    사용자가 채팅에서 고를 수 있는 스킬만 허용한다.
    """
    if not requested_name:
        return ""
    from core.api.skills_router import _scan_skills

    names = {s["name"] for s in _scan_skills(tenant_id)}
    return requested_name if requested_name in names else ""


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_extends_from_skill_md(skill_md_path: str) -> list[str]:
    """SKILL.md 파일에서 frontmatter의 extends 필드를 list[str]로 반환.

    단일 문자열(`extends: parent`) 및 YAML 리스트 양쪽을 지원한다.
    파일이 없거나 파싱 실패 시 빈 리스트를 반환한다.
    """
    try:
        text = Path(skill_md_path).read_text(encoding="utf-8")
    except OSError:
        return []
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return []
    yaml_text = m.group(1)
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        # YAML이 깨진 경우(e.g. description에 비따옴표 콜론) regex로 extends만 추출
        return _extract_extends_regex(yaml_text)
    if not isinstance(data, dict):
        return []
    val = data.get("extends")
    if not val:
        return []
    if isinstance(val, str):
        return [val.strip()] if val.strip() else []
    if isinstance(val, list):
        return [str(v).strip() for v in val if str(v).strip()]
    return []


def _extract_extends_regex(yaml_text: str) -> list[str]:
    """YAML 파싱 실패 시 regex로 extends 값만 추출한다."""
    # single-value: extends: skill-name
    ext_m = re.search(r"^extends:\s*(\S.*)$", yaml_text, re.MULTILINE)
    if ext_m:
        val = ext_m.group(1).strip().strip("\"'")
        return [val] if val else []
    # list format: extends:\n  - item
    ext_list_m = re.search(r"^extends:\s*$", yaml_text, re.MULTILINE)
    if ext_list_m:
        rest = yaml_text[ext_list_m.end():]
        nk = re.search(r"^\S", rest, re.MULTILINE)
        section = rest[: nk.start()] if nk else rest
        items = [i.strip().strip("\"'") for i in re.findall(r"^\s+-\s+(.+)$", section, re.MULTILINE)]
        return [i for i in items if i]
    return []


def _resolve_extends_chain(
    all_skills: list,
    allowed_names: frozenset[str],
) -> list:
    """BFS/DFS 위상정렬로 extends 체인을 해소해 조상→자식 순서의 리스트 반환.

    - allowed_names 에 있는 스킬의 부모를 재귀 탐색해 앞에 삽입한다.
    - 순환 상속은 in_progress 세트로 감지하고 해당 엣지를 무시한다.
    - 부모 스킬이 all_skills 에 없으면 경고 로그 후 무시한다.
    """
    by_name: dict[str, object] = {s["name"]: s for s in all_skills}
    result: list[str] = []
    done: set[str] = set()
    in_progress: set[str] = set()

    def _visit(name: str) -> None:
        if name in done:
            return
        if name not in by_name:
            logger.warning("skill-extends: parent '%s' not found, skipping", name)
            return
        if name in in_progress:
            logger.warning(
                "skill-extends: circular inheritance detected at '%s', breaking cycle", name
            )
            return
        in_progress.add(name)
        for parent in _parse_extends_from_skill_md(by_name[name]["path"]):
            _visit(parent)
        in_progress.discard(name)
        if name not in done:
            done.add(name)
            result.append(name)

    for name in allowed_names:
        _visit(name)

    return [by_name[n] for n in result if n in by_name]


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
        all_skills = update.get("skills_metadata", [])
        update["skills_metadata"] = _resolve_extends_chain(all_skills, self._allowed)
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
