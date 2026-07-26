"""core/web/tools.py — 웹페이지 조회(WebSurfer) 도구.

deepagents 에는 Claude Code 의 WebFetch 에 대응하는 도구가 없었다(전용 백엔드
도구 부재로 bsc-strategy-interview 의 "0단계: 회사 정보 확보"가 URL을 받고도
실제로 조회할 방법이 없었음). 이 모듈은 httpx 로 URL을 가져와 HTML을 읽을 수
있는 텍스트로 정리해 에이전트에게 돌려준다 — 별도 서브 LLM 요약 없이, 호출한
에이전트 자신이 텍스트를 읽고 판단하게 한다(Claude Code의 WebFetch와 달리
자체 프록시 LLM을 두지 않는 단순한 버전).

무거운 브라우저 자동화(playwright 등)는 쓰지 않는다 — 회사 소개 페이지 대부분은
서버 렌더링이라 정적 HTML 조회로 충분하고, 새 브라우저 의존성을 sandbox 이미지에
추가하지 않기 위함이다. 클라이언트 사이드 렌더링만 하는 사이트는 본문이 비어 있을
수 있음을 결과에 명시한다.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Optional

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_MAX_CHARS = 8000
_UA = "Mozilla/5.0 (compatible; ProcessGPT-DeepAgents/1.0; +https://uengine.org)"


def _err(msg: str) -> dict:
    logger.warning("[web tools] %s", msg)
    return {"error": msg}


def _extract_text(raw_html: str) -> tuple[Optional[str], str]:
    """아주 단순한 HTML → 텍스트 변환. bs4 등 무거운 의존성을 추가하지 않기 위해
    표준 라이브러리(re/html)만 사용한다 — 완벽한 파싱이 아니라 "읽을 수 있는 정도"가 목표.
    """
    title_match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.IGNORECASE | re.DOTALL)
    title = html.unescape(title_match.group(1)).strip() if title_match else None

    text = raw_html
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    # 블록 요소 경계에 줄바꿈을 넣어 최소한의 문단 구분을 보존한다.
    text = re.sub(r"</(p|div|li|h[1-6]|br|section|article|tr)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = text.strip()
    return title, text


@tool(parse_docstring=True)
async def fetch_webpage(url: str) -> dict:
    """웹페이지를 가져와 읽을 수 있는 텍스트로 정리해 반환한다(WebSurfer 역할).

    회사 홈페이지처럼 사용자가 준 URL의 실제 내용(사업 소개, 제품/서비스, 고객군 등)을
    파악해야 할 때 사용한다. 정적 HTML만 조회하므로, 자바스크립트로만 렌더링되는
    페이지는 본문이 비거나 부실할 수 있다 — 그 경우 result에 그 사실이 표시된다.

    Args:
        url: 조회할 페이지의 전체 URL(스킴 없으면 https://를 붙인다).

    Returns:
        {"url", "final_url", "title", "text"} 또는 {"error": ...}.
        text는 최대 8000자로 잘린다(본문이 더 길면 "...(생략)"이 붙는다).
    """
    if not url or not url.strip():
        return _err("url이 비어 있습니다.")
    target = url.strip()
    if not re.match(r"^https?://", target, re.IGNORECASE):
        target = f"https://{target}"

    # 일부 회사 도메인은 www 없는 apex(root)의 HTTPS 리스너가 깨져 있거나(TLS 종단
    # 미구성 등) DNS가 www 서브도메인/HTTP로만 정상 동작한다(실제 확인된 사례:
    # https://uengine.org는 연결이 끊기지만 https://www.uengine.org, http://uengine.org는
    # 정상). 이런 인프라 문제로 첫 시도가 연결 자체에서 실패하면(HTTP 오류 응답이 아니라
    # 연결 실패일 때만) www 프리픽스 → 평문 http 순으로 자동 재시도한다.
    candidates = [target]
    m = re.match(r"^(https?)://(?!www\.)([^/]+)(/.*)?$", target, re.IGNORECASE)
    if m:
        scheme, host, rest = m.group(1), m.group(2), m.group(3) or ""
        candidates.append(f"{scheme}://www.{host}{rest}")
    if target.lower().startswith("https://"):
        candidates.append("http://" + target[len("https://"):])

    resp = None
    connect_errors: list[str] = []
    for candidate in candidates:
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, follow_redirects=True, headers={"User-Agent": _UA}
            ) as client:
                resp = await client.get(candidate)
                resp.raise_for_status()
            target = candidate
            break
        except httpx.HTTPStatusError as e:
            return _err(f"페이지 조회 실패 ({e.response.status_code}): {candidate}")
        except httpx.RequestError as e:
            connect_errors.append(f"{candidate} — {e or type(e).__name__}")
            continue
        except Exception as e:  # noqa: BLE001 — 에이전트 실행을 죽이지 않기 위해 전부 흡수
            return _err(f"페이지 조회 중 알 수 없는 오류: {candidate} — {e}")

    if resp is None:
        return _err("페이지 연결 실패(시도한 주소 모두 실패): " + "; ".join(connect_errors))

    content_type = resp.headers.get("content-type", "")
    if "text/html" not in content_type and "text" not in content_type:
        return _err(f"HTML 문서가 아닙니다(content-type: {content_type}): {target}")

    title, text = _extract_text(resp.text)
    truncated = len(text) > _MAX_CHARS
    if truncated:
        text = text[:_MAX_CHARS] + "\n...(생략 — 본문이 더 깁니다)"

    result = {
        "url": target,
        "final_url": str(resp.url),
        "title": title,
        "text": text,
    }
    if not text:
        result["notice"] = (
            "본문이 비어 있습니다 — 이 페이지가 자바스크립트로만 렌더링되는 경우일 수 있습니다. "
            "회사/제품/서비스 소개 하위 페이지 URL을 사용자에게 다시 물어보세요."
        )
    return result


def get_web_tools() -> list:
    return [fetch_webpage]
