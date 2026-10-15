import logging
import os
import re
from pathlib import Path

from typing import Annotated, NotRequired

import yaml
from deepagents.middleware.skills import SkillsMiddleware, SkillsState, SkillsStateUpdate
from langchain.agents.middleware.types import OmitFromOutput

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


def scan_skill_include_tools(source_dirs) -> dict[str, dict]:
    """스킬 소스에서 `{스킬명: {"name", "path", "include_tools"}}` 를 읽는다.

    SkillsMiddleware 와 같은 규칙이다 — 소스 바로 아래 디렉터리마다 SKILL.md 하나, 같은
    이름이면 뒤 소스가 이긴다. `include_tools` 는 frontmatter `metadata.include_tools` 의
    공백 구분 도구 이름이다(deepagents skill tools). `path` 는 호스트의 SKILL.md 경로다.
    """
    result: dict[str, dict] = {}
    for source in source_dirs or []:
        try:
            entries = sorted(p for p in Path(source).iterdir() if p.is_dir())
        except OSError:
            continue
        for entry in entries:
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            data: dict = {}
            try:
                m = _FRONTMATTER_RE.match(skill_md.read_text(encoding="utf-8"))
                loaded = yaml.safe_load(m.group(1)) if m else None
                data = loaded if isinstance(loaded, dict) else {}
            except (OSError, yaml.YAMLError):
                pass
            metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            raw = metadata.get("include_tools")
            name = str(data.get("name") or entry.name)
            result[name] = {
                "name": name,
                "path": str(skill_md),
                "include_tools": raw.split() if isinstance(raw, str) else [],
            }
    return result


def skill_sources_fingerprint(host_sources, sources) -> dict[str, str]:
    """스킬 소스의 `{SKILL.md 경로: "수정시각:크기"}` — 파일을 읽지 않고 stat 만 한다.

    SkillsMiddleware 와 같은 규칙(소스 바로 아래 디렉터리마다 SKILL.md)으로 본다. 키는
    `sources`(에이전트가 보는 경로) 기준이라 `skills_metadata` 의 `path` 와 같다.
    `host_sources` 는 같은 순서의 호스트 경로다(샌드박스 마운트 전 실제 위치).
    """
    result: dict[str, str] = {}
    for host, source in zip(host_sources, sources):
        try:
            entries = sorted(e.name for e in os.scandir(host) if e.is_dir())
        except OSError:
            continue
        for name in entries:
            try:
                st = os.stat(os.path.join(host, name, "SKILL.md"))
            except OSError:
                continue
            result[f"{source.rstrip('/')}/{name}/SKILL.md"] = f"{st.st_mtime_ns}:{st.st_size}"
    return result


class SkillReloadState(SkillsState):
    skills_fingerprint: NotRequired[Annotated[dict[str, str] | None, OmitFromOutput]]
    """`skills_metadata` 를 읽던 때의 `skill_sources_fingerprint`. 다르면 스킬이 바뀐 것이다.

    실행 입력으로 받아야 하므로 `skills_metadata` 와 같이 입력에서는 보이고 출력에서만 뺀다.
    """


def skill_reload_inputs(checkpoint_values, host_sources, sources) -> dict:
    """이번 실행 입력에 더할 스킬 재로드 값(deepagents Reload skills).

    SkillsMiddleware 는 thread 마다 한 번 읽은 목록을 `skills_metadata` 에 두고 체크포인트로
    이어 쓴다. 공식 방법은 다시 읽힐 실행의 입력에 `skills_metadata: None` 을 넣는 것이다.
    언제 넣을지는 앱이 정하므로, SKILL.md 의 stat 이 그 목록을 읽던 때(체크포인트의
    `skills_fingerprint`)와 다를 때만 넣는다 — 바뀌지 않은 턴에는 다시 스캔하지 않는다.
    `checkpoint_values` 는 이 thread 의 마지막 체크포인트 값(없으면 빈 dict)이다.
    """
    fingerprint = skill_sources_fingerprint(host_sources, sources)
    values = checkpoint_values or {}
    if values.get("skills_metadata") is None:
        # 아직 읽지 않은 thread — SkillsMiddleware 가 이번 실행에서 읽는다. 기준만 남긴다.
        return {"skills_fingerprint": fingerprint}
    before = values.get("skills_fingerprint")
    if before == fingerprint:
        return {}
    before = before or {}
    logger.info(
        "[skill-reload] 스킬 파일이 바뀌어 이번 실행에서 다시 읽음 added=%s removed=%s modified=%s",
        sorted(fingerprint.keys() - before.keys()),
        sorted(before.keys() - fingerprint.keys()),
        sorted(k for k in fingerprint.keys() & before.keys() if fingerprint[k] != before[k]),
    )
    return {"skills_metadata": None, "skills_fingerprint": fingerprint}


class ToolBindingSkillsMiddleware(SkillsMiddleware):
    """`tools` 로 받은 스킬 바인딩 도구가 실제 모델 요청에 실렸는지 기록하는 SkillsMiddleware.

    이름을 `SkillsMiddleware` 로 두어 create_deep_agent 의 기본 SkillsMiddleware 자리를
    대신한다(요약 뒤·프롬프트 캐싱 앞, 범용 서브에이전트에도 상속). 기록은 deepagents 가
    도구 공개를 마친 뒤의 요청을 본다 — 스킬을 읽기 전에는 없고 읽은 뒤에 생겼는지를 실제
    실행 로그로 확인하기 위한 것이다.

    상태에 `skills_fingerprint` 를 더한다 — 스킬 재로드 여부는 executor 가 실행 입력으로
    정한다(`skill_reload_inputs`). 목록을 읽는 일은 공식 SkillsMiddleware 그대로다.
    """

    state_schema = SkillReloadState

    def __init__(self, *, backend, sources, tools=None, **kwargs):
        super().__init__(backend=backend, sources=sources, tools=tools, **kwargs)
        self._bound_names = frozenset(getattr(t, "name", "") for t in (tools or []) if getattr(t, "name", ""))

    @property
    def name(self) -> str:
        return "SkillsMiddleware"

    def wrap_model_call(self, request, handler):
        def _logged(req):
            self._log_request(req)
            return handler(req)

        return super().wrap_model_call(request, _logged)

    async def awrap_model_call(self, request, handler):
        async def _logged(req):
            self._log_request(req)
            return await handler(req)

        return await super().awrap_model_call(request, _logged)

    def _log_request(self, request) -> None:
        names = [_request_tool_name(t) for t in (request.tools or [])]
        inline = [
            _inline_tool_name(block)
            for message in (request.messages or [])
            if getattr(message, "type", "") == "system" and isinstance(message.content, list)
            for block in message.content
            if isinstance(block, dict)
        ]
        visible = sorted({n for n in (*names, *inline) if n in self._bound_names})
        logger.debug("[skill-tools] %s model request tool names=%s", self.name, sorted(n for n in (*names, *inline) if n))
        logger.info(
            "[skill-tools] %s model request tools=%d bound_visible=%s bound_total=%d",
            self.name, len(names) + len([n for n in inline if n]), visible, len(self._bound_names),
        )


def _request_tool_name(tool) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        return tool.get("name") or (function.get("name") if isinstance(function, dict) else "") or ""
    return getattr(tool, "name", "") or ""


def _inline_tool_name(block: dict) -> str:
    """대화 중간 도구 추가 블록(Anthropic `tool_addition`, OpenAI `additional_tools`)이 싣는 도구 이름."""
    if block.get("type") == "tool_addition":
        return ((block.get("tool") or {}).get("definition") or {}).get("name") or ""
    if block.get("type") == "additional_tools":
        return ((block.get("tools") or [{}])[0]).get("name") or ""
    return ""


class FilteredSkillsMiddleware(ToolBindingSkillsMiddleware):
    """SkillsMiddleware that restricts visible skills to a specified set.

    Loads skills from the full collection but filters state so only the
    allowed skill names appear in the system prompt. Skills not in
    `allowed_skills` are invisible to the sub-agent.
    """

    # 기본 SkillsMiddleware 자리를 대신하지 않는다 — 대신하면 create_deep_agent 가 그 소스로
    # 범용 서브에이전트에 필터 없는 스킬을 붙인다. 지금까지와 같은 자리·이름을 유지한다.
    @property
    def name(self) -> str:
        return "FilteredSkillsMiddleware"

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
