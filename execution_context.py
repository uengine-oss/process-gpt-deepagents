"""실행 시각·타임존을 고정해 자연어(어제, 오늘 등) 해석과 스킬 매핑을 안정화한다."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def get_execution_timezone() -> ZoneInfo:
    name = (os.environ.get("EXECUTION_TZ") or os.environ.get("TZ") or "Asia/Seoul").strip()
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Seoul")


def format_execution_clock_section() -> str:
    """프롬프트에 붙이는 고정 시각 블록(스킬 비특화)."""
    tz = get_execution_timezone()
    now = datetime.now(tz)
    today = now.date()
    yesterday = today - timedelta(days=1)
    return (
        "=== Execution clock (server-injected — use for every date / relative phrase) ===\n"
        f"- Timezone: {tz.key}\n"
        f"- Now (ISO): {now.isoformat(timespec='seconds')}\n"
        f"- Today's calendar date: {today.isoformat()}\n"
        f"- Calendar \"yesterday\" in this timezone: {yesterday.isoformat()} "
        "(the calendar day before Today).\n"
        "- Natural language → concrete values:\n"
        "  * Map \"어제\" / \"yesterday\" / \"전일\" to dates using the lines above first.\n"
        "  * For **market data** (종가, 전일 종가, previous close), calendar yesterday may differ from "
        "the last **trading** day — follow the **loaded skill's** docs/scripts (references, APIs, examples) "
        "for how that data source defines the as-of date; do not guess JSON keys or HTTP paths.\n"
        "- Skill invocation (no per-skill hardcoding in agent code):\n"
        "  * Prefer the skill's bundled CLI/scripts and **exact** flags shown in `read_skill_document`, "
        "`--help`, or examples — **never invent flags** (e.g. do not add options that argparse rejects).\n"
        "  * If the user request implies a date window, translate it using this clock, then pass parameters "
        "in the form the skill's interface actually supports.\n"
        "- Debugging: if a call fails, distinguish **wrong field names / response shape** from "
        "**missing API keys**; many public endpoints need no secret — fix parsing and flags first.\n"
    )
