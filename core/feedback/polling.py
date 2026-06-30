"""core/feedback/polling.py — 피드백 작업 폴링 매니저.

Supabase의 agent_feedback_task RPC를 주기적으로 호출해
피드백이 있는 todolist 항목을 가져와 처리한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from core.chat.activity import load_activity_capabilities
from core.feedback.db import (
    fetch_events_by_todo_id,
    fetch_feedback_task,
    get_agent_by_id,
    get_user_by_id,
    update_feedback_status,
)
from core.feedback.processor import match_feedback_to_agents
from core.feedback.agent import process_feedback_with_deep_agent

logger = logging.getLogger(__name__)


async def _get_agents_info(user_ids: str, assignees: Any = None) -> list[dict[str, Any]]:
    agent_list: list[dict[str, Any]] = []
    found: set[str] = set()

    if user_ids:
        for uid in [u.strip() for u in user_ids.split(",") if u.strip()]:
            info = get_agent_by_id(uid)
            if info:
                agent_list.append(info)
                found.add(uid)

    if not agent_list and assignees:
        try:
            data = json.loads(assignees) if isinstance(assignees, str) else assignees
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        endpoints = item.get("endpoint") or []
                        if isinstance(endpoints, str):
                            endpoints = [endpoints]
                        for eid in endpoints:
                            if eid and eid not in found:
                                info = get_agent_by_id(eid)
                                if info:
                                    agent_list.append(info)
                                    found.add(eid)
        except Exception:
            logger.warning("assignees 파싱 실패 (무시)", exc_info=True)

    return agent_list


def _extract_latest_feedback(feedback_raw: Any) -> tuple[str, str]:
    """최신 피드백의 (content, user_id)를 반환한다."""
    if isinstance(feedback_raw, list):
        try:
            if feedback_raw:
                sorted_fb = sorted(feedback_raw, key=lambda x: x.get("time", ""), reverse=True)
                latest = sorted_fb[0]
                if isinstance(latest, dict):
                    return (latest.get("content", ""), str(latest.get("user_id") or "").strip())
                return (str(latest), "")
        except Exception:
            return ("", "")
    elif isinstance(feedback_raw, str):
        return (feedback_raw, "")
    elif feedback_raw:
        return (str(feedback_raw), "")
    return ("", "")


async def process_feedback_task(row: dict[str, Any]) -> None:
    todo_id = row["id"]
    user_ids = row.get("user_id", "")
    assignees = row.get("assignees")
    feedback, feedback_user_id = _extract_latest_feedback(row.get("feedback", ""))
    description = row.get("description", "")
    tenant_id = str(row.get("tenant_id") or "").strip()
    proc_def_id = str(row.get("proc_def_id") or "").strip()
    activity_id = str(row.get("activity_id") or "").strip()

    # 피드백 작성자 정보 (git 커밋/PR의 요청자로 사용)
    requester_id = feedback_user_id or None
    requester_name: str | None = None
    requester_email: str | None = None
    if feedback_user_id:
        requester_info = get_user_by_id(feedback_user_id)
        if requester_info:
            requester_name = requester_info.get("username") or requester_info.get("name")
            requester_email = requester_info.get("email")

    try:
        await update_feedback_status(todo_id, "PROCESSING")
        logger.info("피드백 처리 시작: todo_id=%s requester_id=%s", todo_id, requester_id)

        agents = await _get_agents_info(user_ids, assignees)

        # 액티비티에 설정된 스킬 조회
        _, activity_skills = load_activity_capabilities(
            tenant_id=tenant_id,
            proc_def_id=proc_def_id,
            activity_id=activity_id,
        )

        if not agents and not activity_skills:
            logger.warning("에이전트 정보·액티비티 스킬 모두 없음: user_ids=%s", user_ids)
            await update_feedback_status(todo_id, "FAILED")
            return

        events = await fetch_events_by_todo_id(todo_id)
        had_error = False

        if agents:
            # 에이전트가 있으면 LLM 매칭 후 에이전트별 처리
            matching = await match_feedback_to_agents(feedback, agents, description, events)
            agent_feedbacks = matching.get("agent_feedbacks", [])

            if not agent_feedbacks:
                logger.info("매칭된 피드백 없음: todo_id=%s", todo_id)
                await update_feedback_status(todo_id, "COMPLETED")
                return

            for fb_item in agent_feedbacks:
                aid = fb_item.get("agent_id")
                aname = fb_item.get("agent_name", "Unknown")
                lc = fb_item.get("learning_candidate", {})
                if not lc:
                    continue

                agent_info = get_agent_by_id(aid)
                if not agent_info:
                    continue

                try:
                    result = await process_feedback_with_deep_agent(
                        agent_id=aid,
                        agent_info=agent_info,
                        feedback_content=lc.get("content", ""),
                        task_description=description,
                        events=events,
                        activity_skills=activity_skills,
                        requester_id=requester_id,
                        requester_name=requester_name,
                        requester_email=requester_email,
                    )
                    if result.get("error"):
                        had_error = True
                        logger.warning("에이전트 %s 처리 에러: %s", aname, str(result["error"])[:200])
                except Exception:
                    had_error = True
                    logger.warning("에이전트 %s 피드백 처리 실패", aname, exc_info=True)
        else:
            # 에이전트 없이 액티비티 스킬만 있는 경우
            logger.info("에이전트 없음, 액티비티 스킬로 처리: todo_id=%s skills=%s", todo_id, activity_skills)
            try:
                result = await process_feedback_with_deep_agent(
                    agent_id="",
                    agent_info={"tenant_id": tenant_id},
                    feedback_content=feedback,
                    task_description=description,
                    events=events,
                    activity_skills=activity_skills,
                    requester_id=requester_id,
                    requester_name=requester_name,
                    requester_email=requester_email,
                )
                if result.get("error"):
                    had_error = True
                    logger.warning("액티비티 스킬 처리 에러: %s", str(result["error"])[:200])
            except Exception:
                had_error = True
                logger.warning("액티비티 스킬 피드백 처리 실패", exc_info=True)

        await update_feedback_status(todo_id, "FAILED" if had_error else "COMPLETED")
        logger.info("피드백 처리 완료: todo_id=%s", todo_id)

    except Exception:
        logger.warning("피드백 작업 처리 에러: todo_id=%s", todo_id, exc_info=True)
        await update_feedback_status(todo_id, "FAILED")


async def start_feedback_polling(interval: int = 7) -> None:
    """피드백 작업 폴링을 시작한다. 서버 수명 동안 무한 루프로 실행된다."""
    logger.info("피드백 폴링 시작 (interval=%ds)", interval)

    while True:
        try:
            row = await fetch_feedback_task()
            if row:
                await process_feedback_task(row)
        except asyncio.CancelledError:
            logger.info("피드백 폴링 종료")
            break
        except Exception:
            logger.warning("폴링 중 에러 (계속 진행)", exc_info=True)

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
