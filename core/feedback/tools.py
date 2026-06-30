"""core/feedback/tools.py — 피드백 에이전트용 스킬 도구.

쓰기 원칙:
  - 비-Git 스킬 생성/수정 → workspace/feedback-skills/ 에 결과물 저장
  - Git 연동 스킬 수정 → 피처 브랜치 + PR 워크플로우
  - 스킬 내용(SKILL.md)은 에이전트가 skill-creator 가이드라인에 따라 작성
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from langchain_core.tools import tool

from core.feedback.db import (
    get_agent_by_id,
    record_feedback_pr,
    update_agent_and_tenant_skills,
)

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_KV_RE = re.compile(r"^([^:\n]+?)\s*:\s*(.+)$", re.MULTILINE)

_WORKSPACE_ROOT = Path(
    os.environ.get("WORKSPACE", str(Path(__file__).parent.parent.parent / "workspace"))
).resolve()
_FEEDBACK_SKILLS_DIR = _WORKSPACE_ROOT / "feedback-skills"


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    return {k.strip(): v.strip() for k, v in _KV_RE.findall(m.group(1))}


def _parse_skill_ids_input(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = raw.get("skill_ids", "") or ""
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                raw = obj.get("skill_ids", "") or ""
            elif isinstance(obj, list):
                raw = ",".join(str(x) for x in obj)
        except (json.JSONDecodeError, TypeError):
            pass
    return [s.strip() for s in str(raw).split(",") if s.strip()]


# ---------------------------------------------------------------------------
# 읽기 전용 스킬 조회 헬퍼
# ---------------------------------------------------------------------------

def _list_tenant_skills(tenant_id: str) -> list[dict[str, Any]]:
    from core.api.skills_router import _scan_skills
    return _scan_skills(tenant_id)


def _find_skill(skill_name: str, tenant_id: str = "") -> Path | None:
    from core.api.skills_router import _find_skill_dir
    config_id = ""
    if tenant_id:
        try:
            from core.api.skills_router import _get_provider
            provider = _get_provider(tenant_id)
            config_id = provider.config_id
        except Exception:
            pass
    return _find_skill_dir(skill_name, tenant_id, config_id)


# ---------------------------------------------------------------------------
# workspace 쓰기 헬퍼
# ---------------------------------------------------------------------------

def _write_to_workspace(skill_name: str, content: str) -> Path:
    """workspace/feedback-skills/{skill_name}/SKILL.md 에 저장하고 경로를 반환한다."""
    dest = _FEEDBACK_SKILLS_DIR / skill_name
    dest.mkdir(parents=True, exist_ok=True)
    md = dest / "SKILL.md"
    md.write_text(content, encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# Git PR 워크플로우
# ---------------------------------------------------------------------------

def _try_git_pr_workflow(
    tenant_id: str,
    skill_id: str,
    content: str,
    agent_id: str,
    requester_id: str | None = None,
    requester_name: str | None = None,
    requester_email: str | None = None,
) -> str | None:
    """git repo가 있는 스킬이면 피처 브랜치 + PR을 생성한다. 비대상이거나 실패하면 None."""
    if not tenant_id:
        return None

    try:
        from core.skills.git_providers.factory import get_provider
        provider = get_provider(tenant_id)
    except Exception:
        return None

    if not provider.repo_exists(skill_id):
        return None

    owner = provider.username
    try:
        default_branch = provider.detect_default_branch(owner, skill_id)
    except Exception:
        default_branch = "main"

    branch = f"feedback/{skill_id}/{int(time.time())}"

    try:
        provider.create_branch(owner, skill_id, branch, default_branch)
    except Exception as e:
        logger.warning("피드백 PR 브랜치 생성 실패: skill=%s: %s", skill_id, e)
        return None

    commit_msg = f"feedback: update {skill_id}"
    try:
        provider.update_file(
            owner, skill_id, "SKILL.md", content, commit_msg, branch,
            author_name=requester_name, author_email=requester_email,
        )
    except Exception as e:
        logger.warning("피드백 PR 파일 커밋 실패: skill=%s branch=%s: %s", skill_id, branch, e)
        return None

    pr_title = f"[Feedback] {skill_id} 스킬 개선"
    pr_body = f"피드백 기반 자동 스킬 개선 PR입니다."

    try:
        pr = provider.create_pull_request(owner, skill_id, pr_title, pr_body, branch, default_branch)
    except Exception as e:
        logger.warning("피드백 PR 생성 실패: skill=%s branch=%s: %s", skill_id, branch, e)
        repo_url = provider.get_repo_html_url(owner, skill_id)
        return (
            f"피처 브랜치에 커밋되었지만 PR 생성에 실패했습니다.\n"
            f"브랜치: {branch}\n레포: {repo_url}\n수동으로 PR을 생성해 주세요."
        )

    pr_number = pr.get("number")
    pr_url = pr.get("html_url", "")
    repo_url = provider.get_repo_html_url(owner, skill_id)

    record_feedback_pr(
        tenant_id=tenant_id,
        skill_name=skill_id,
        branch_name=branch,
        base_branch=default_branch,
        title=pr_title,
        description=pr_body,
        requester_id=requester_id or agent_id,
        requester_name=requester_name,
        git_pr_number=pr_number,
        git_pr_url=pr_url,
        git_repo_url=repo_url,
    )

    return (
        f"피드백 기반 PR이 생성되었습니다.\n"
        f"PR #{pr_number}: {pr_url}\n"
        f"브랜치: {branch} → {default_branch}\n"
        f"스킬 주인이 리뷰 후 병합 여부를 결정할 수 있습니다."
    )


# ---------------------------------------------------------------------------
# 도구 팩토리
# ---------------------------------------------------------------------------

def _has_repo(skill_name: str, tenant_id: str) -> bool:
    """스킬이 git repo에 연결되어 있는지 확인한다."""
    try:
        all_skills = _list_tenant_skills(tenant_id)
        for sk in all_skills:
            if sk["name"] == skill_name:
                return sk.get("has_repo", False)
    except Exception:
        pass
    return False


def create_feedback_skill_tools(
    agent_id: str,
    feedback_content: str | None = None,
    activity_skills: list[str] | None = None,
    tenant_id: str = "",
    requester_id: str | None = None,
    requester_name: str | None = None,
    requester_email: str | None = None,
) -> list:
    """agent_id가 바인딩된 피드백 처리용 스킬 도구 4개를 반환한다."""

    @tool
    async def list_agent_skills() -> str:
        """이 에이전트에 적재된 스킬과 액티비티에 설정된 스킬 목록을 조회합니다.

        각 스킬의 이름, 설명, Git 연동 여부, 출처(에이전트/액티비티)를 반환합니다.
        상세 내용을 확인하려면 get_skill_detail을 사용하세요.
        """
        try:
            agent_info = get_agent_by_id(agent_id) if agent_id else None

            _tid = (agent_info.get("tenant_id", "") if agent_info else "") or tenant_id
            agent_skills_text = (agent_info.get("skills", "") if agent_info else "") or ""
            agent_skill_names = [s.strip() for s in agent_skills_text.split(",") if s.strip()]
            agent_skill_set = set(agent_skill_names)

            # 액티비티에 설정된 스킬 병합 (중복 제거)
            activity_only: list[str] = []
            if activity_skills:
                for sn in activity_skills:
                    if sn not in agent_skill_set:
                        activity_only.append(sn)

            all_skill_names = agent_skill_names + activity_only

            if not all_skill_names:
                return "에이전트에 적재된 스킬과 액티비티에 설정된 스킬이 없습니다."

            results: list[dict[str, Any]] = []
            for sn in all_skill_names:
                sd = _find_skill(sn, _tid)
                desc = ""
                if sd is not None:
                    md_file = sd / "SKILL.md"
                    if md_file.is_file():
                        try:
                            fm = _parse_frontmatter(md_file.read_text(encoding="utf-8"))
                            desc = fm.get("description", "")
                        except Exception:
                            pass
                source = "액티비티" if sn in activity_only else "에이전트"
                results.append({
                    "name": sn,
                    "description": desc,
                    "has_repo": _has_repo(sn, _tid),
                    "found": sd is not None,
                    "source": source,
                })

            lines = [f"스킬 목록 ({len(results)}개):\n"]
            for idx, item in enumerate(results, 1):
                repo_tag = " [Git]" if item["has_repo"] else ""
                found_tag = "" if item["found"] else " [경로 없음]"
                source_tag = f" ({item['source']})"
                lines.append(f"[{idx}] {item['name']}{repo_tag}{source_tag}{found_tag}")
                if item.get("description"):
                    lines.append(f"    설명: {item['description'][:200]}")
                lines.append("")

            lines += [
                "━" * 50,
                "다음 단계:",
                "- get_skill_detail로 개선 대상 스킬의 상세 내용을 확인하세요.",
                "- 피드백 내용에 따라 어떤 스킬을 수정할지 판단하세요.",
            ]
            return "\n".join(lines)
        except Exception as e:
            logger.warning("list_agent_skills 실패", exc_info=True)
            return f"적재 스킬 조회 실패: {e}"

    @tool
    async def get_skill_detail(skill_name: str) -> str:
        """스킬의 전체 상세 내용(SKILL.md + 부가 파일)을 조회합니다.

        Args:
            skill_name: 조회할 스킬 이름/ID
        """
        try:
            agent_info = get_agent_by_id(agent_id) if agent_id else None
            _tid = (agent_info.get("tenant_id", "") if agent_info else "") or tenant_id
            skill_dir = _find_skill(skill_name, _tid)
            if skill_dir is None:
                return f"스킬을 찾을 수 없습니다: {skill_name}"

            lines = [f"스킬 상세 조회: {skill_name}\n"]

            md_file = skill_dir / "SKILL.md"
            if md_file.is_file():
                md_content = md_file.read_text(encoding="utf-8")
                fm = _parse_frontmatter(md_content)
                lines.append(f"이름: {fm.get('name', skill_name)}")
                if fm.get("description"):
                    lines.append(f"설명: {fm['description']}")
                lines += ["\nSKILL.md 내용:", "```markdown", md_content, "```"]

            extra_files = [
                p for p in sorted(skill_dir.rglob("*"))
                if p.is_file() and p.name != "SKILL.md" and ".git" not in p.parts
            ]
            if extra_files:
                lines.append(f"\n부가 파일 ({len(extra_files)}개):")
                for fp in extra_files:
                    rel = str(fp.relative_to(skill_dir))
                    size = fp.stat().st_size
                    try:
                        fc = fp.read_text(encoding="utf-8")
                        ext = fp.suffix.lstrip(".") or "text"
                        lines += [f"\n{rel} ({size} bytes):", f"```{ext}", fc, "```"]
                    except UnicodeDecodeError:
                        lines.append(f"{rel} ({size} bytes, binary)")

            return "\n".join(lines)
        except Exception as e:
            logger.warning("get_skill_detail 실패", exc_info=True)
            return f"스킬 상세 조회 실패: {e}"

    @tool
    async def commit_to_skill(
        operation: str = "CREATE",
        skill_id: Optional[str] = None,
        content: Optional[str] = None,
    ) -> str:
        """스킬을 생성/수정/삭제합니다.

        content에는 skill-creator 가이드라인에 따라 작성한 전체 SKILL.md 내용을 전달하세요.
        YAML frontmatter(name, description)와 Markdown body를 포함해야 합니다.

        - Git 연동 스킬 UPDATE: 피처 브랜치 + PR 생성 (스킬 주인이 리뷰 후 병합 결정)
        - 비-Git 스킬: workspace/feedback-skills/ 에 결과물 저장

        Args:
            operation: 작업 타입 (CREATE | UPDATE | DELETE)
            skill_id: 스킬 이름 (CREATE/UPDATE/DELETE 모두 필수)
            content: skill-creator 가이드라인에 따라 작성한 전체 SKILL.md 내용 (CREATE/UPDATE 시 필수)
        """
        try:
            agent_info = get_agent_by_id(agent_id) if agent_id else None
            _tid = (agent_info.get("tenant_id", "") if agent_info else "") or tenant_id

            # --- DELETE ---
            if operation == "DELETE":
                if not skill_id or not skill_id.strip():
                    return "DELETE에는 skill_id(기존 스킬 이름)가 필요합니다."

                if agent_id:
                    update_agent_and_tenant_skills(agent_id, skill_id, "DELETE")
                return f"스킬 연결이 해제되었습니다. (ID: {skill_id}, 에이전트: {agent_id})"

            # --- content 검증 ---
            if not content or not content.strip():
                return "CREATE/UPDATE에는 content(SKILL.md 전체 내용)가 필요합니다. skill-creator 가이드라인에 따라 작성하세요."

            if not skill_id or not skill_id.strip():
                return "skill_id(스킬 이름)가 필요합니다."

            # --- UPDATE ---
            if operation == "UPDATE":
                skill_dir = _find_skill(skill_id, _tid)
                if skill_dir is None:
                    logger.info("UPDATE 대상 없음, CREATE로 전환: %s", skill_id)
                    operation = "CREATE"
                else:
                    pr_result = _try_git_pr_workflow(
                        _tid, skill_id, content, agent_id,
                        requester_id=requester_id, requester_name=requester_name,
                        requester_email=requester_email,
                    )
                    if pr_result:
                        return pr_result

                    dest = _write_to_workspace(skill_id, content)
                    return f"스킬 수정본이 workspace에 저장되었습니다. (경로: {dest})"

            # --- CREATE ---
            if operation == "CREATE":
                dest = _write_to_workspace(skill_id, content)
                if agent_id:
                    update_agent_and_tenant_skills(agent_id, skill_id, "CREATE")
                return f"스킬이 workspace에 생성되었습니다. (경로: {dest})"

            return f"알 수 없는 작업: {operation}"
        except Exception as e:
            logger.warning("commit_to_skill 실패", exc_info=True)
            return f"스킬 저장 실패: {e}"

    @tool
    async def attach_skills_to_agent(skill_ids: str) -> str:
        """기존 스킬을 에이전트에 적재합니다. 스킬 내용은 변경하지 않습니다.
        액티비티 스킬은 적재 대상이 아닙니다 — 이미 프로세스 정의에 설정되어 있습니다.

        Args:
            skill_ids: 에이전트에 적재할 스킬 이름 (쉼표 구분, 예: 'skill-a, skill-b')
        """
        if not agent_id:
            return "에이전트가 지정되지 않은 작업입니다. 액티비티 스킬은 에이전트에 적재하지 않습니다. commit_to_skill로 스킬 내용을 개선하세요."

        try:
            skill_names = _parse_skill_ids_input(skill_ids)
            if not skill_names:
                return "skill_ids가 비어있습니다."

            attached = []
            for sn in skill_names[:10]:
                try:
                    update_agent_and_tenant_skills(agent_id, sn, "CREATE")
                    attached.append(sn)
                except Exception:
                    logger.warning("스킬 적재 실패: %s", sn, exc_info=True)

            if not attached:
                return f"스킬 적재 실패: {', '.join(skill_names)}"
            return f"기존 스킬 {len(attached)}개를 에이전트에 적재했습니다: {', '.join(attached)}"
        except Exception as e:
            logger.warning("attach_skills_to_agent 실패", exc_info=True)
            return f"스킬 적재 실패: {e}"

    return [list_agent_skills, get_skill_detail, commit_to_skill, attach_skills_to_agent]
