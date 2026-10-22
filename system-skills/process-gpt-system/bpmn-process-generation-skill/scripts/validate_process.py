"""검증 보조 모듈 (LLM 자동교정 호출자만 제공).

실제 실행엔진 검증(=proc_def 임시행 생성 → /initiate·/complete 구동)은 이 스킬에서
제거했다. 스킬·에이전트가 Supabase 에 직접 쓰지 않는다는 원칙에 따라, 임시저장(draft)과
실행엔진 검증은 프론트가 사용자 자격증명으로 `/validate-and-improve` 를 호출해 수행한다.

여기 남은 것은 deepagent 의 `validate_process_definition` 툴이 정적 결함을 LLM 으로
자동교정할 때 쓰는 모델 호출자(`_make_llm_call`)뿐이다. DB 접근은 없다.

환경변수:
  LLM_PROXY_URL / LLM_PROXY_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY
  LLM_MODEL                        자동교정 모델 (없으면 교정 없이 검증만)
"""

from __future__ import annotations

import os
import json
import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("bpmn.validate")


# ---------------------------------------------------------------------------
# 주입 의존성
# ---------------------------------------------------------------------------
def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    t = text.strip()
    if "```" in t:
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.DOTALL)
        if m:
            t = m.group(1)
    s, e = t.find("{"), t.rfind("}")
    if s >= 0 and e > s:
        t = t[s:e + 1]
    try:
        return json.loads(t)
    except Exception:
        return None


def _make_llm_call():
    """async (messages, max_tokens) -> dict|None.

    ProcessGPT deepagent(core/model.py)와 동일한 우선순위로 LLM 을 고른다:
      1) LLM_PROXY_URL + LLM_PROXY_API_KEY (OpenAI 호환 프록시)
      2) ANTHROPIC_API_KEY
      3) OPENAI_API_KEY
    하나도 없으면 자동교정 없이 검증만(noop 반환).
    """
    model = os.getenv("LLM_MODEL")
    proxy_url = os.getenv("LLM_PROXY_URL")
    proxy_key = os.getenv("LLM_PROXY_API_KEY")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")

    def _noop(reason):
        async def _f(messages, max_tokens):
            logger.info("[VALIDATE] 자동교정 LLM 비활성: %s", reason)
            return None
        return _f

    # --- OpenAI 호환 (프록시 또는 OPENAI_API_KEY) ---
    def _make_openai_call(*, base_url, api_key, mdl):
        try:
            from openai import OpenAI  # type: ignore
        except Exception:
            return _noop("openai 패키지 없음")
        client = OpenAI(api_key=api_key, base_url=base_url) if base_url \
            else OpenAI(api_key=api_key)

        async def _call(messages: List[Dict[str, Any]], max_tokens: int) -> Optional[dict]:
            def _do():
                resp = client.chat.completions.create(
                    model=mdl or "gpt-4o",
                    messages=messages,  # system/user role 그대로 사용
                    max_completion_tokens=int(max_tokens or 4000),
                )
                return resp.choices[0].message.content or ""
            try:
                text = await asyncio.to_thread(_do)
                return _extract_json(text)
            except Exception as e:
                logger.warning("[VALIDATE] LLM(OpenAI호환) 호출 실패: %s", e)
                return None
        return _call

    if proxy_url and proxy_key:
        return _make_openai_call(base_url=proxy_url, api_key=proxy_key, mdl=model)

    if anthropic_key:
        try:
            import anthropic  # type: ignore
        except Exception:
            return _noop("anthropic 패키지 없음")
        client = anthropic.Anthropic(api_key=anthropic_key)

        async def _call_anthropic(messages: List[Dict[str, Any]], max_tokens: int) -> Optional[dict]:
            sys_parts = [m["content"] for m in messages if m.get("role") == "system"]
            chat = [m for m in messages if m.get("role") != "system"]
            system = "\n\n".join(sys_parts) if sys_parts else None

            def _do():
                kwargs = dict(model=model or "claude-sonnet-4-6",
                              max_tokens=int(max_tokens or 4000), messages=chat)
                if system:
                    kwargs["system"] = system
                resp = client.messages.create(**kwargs)
                return "".join(getattr(b, "text", "") for b in resp.content)
            try:
                text = await asyncio.to_thread(_do)
                return _extract_json(text)
            except Exception as e:
                logger.warning("[VALIDATE] LLM(Anthropic) 호출 실패: %s", e)
                return None
        return _call_anthropic

    if openai_key:
        return _make_openai_call(base_url=None, api_key=openai_key, mdl=model)

    return _noop("LLM 자격증명 없음(LLM_PROXY_*/ANTHROPIC_API_KEY/OPENAI_API_KEY)")
