"""채팅 첨부 이미지를 모델이 볼 수 있는 이미지 입력으로 옮긴다.

프론트는 첨부 이미지를 본문 뒤 `[InputData]` JSON 의 `images` 로 싣는다.
로컬은 `data:image/...;base64,...`, 운영은 Storage 주소다. 이것을 글로 두면
모델은 base64 문자열이나 주소 글자만 받아 이미지 안의 내용을 읽지 못한다
(큰 값은 작업 폴더의 텍스트 파일로 옮겨져 더욱 그렇다).

그래서 채팅 턴에서는 `images` 를 본문에서 떼어 내고, 이번 사용자 메시지에
`image_url` 블록으로 붙인다. 주소는 서버에서 내려받아 data URL 로 바꾼다.
서명 주소는 1시간이면 만료되고, 로컬 Storage 주소는 모델 API 가 열 수 없으며,
대화 이력(체크포인트)에 남은 이미지는 다음 턴에도 다시 보내지기 때문이다.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_INPUT_DATA_RE = re.compile(r"\[InputData\]\s*\n([\s\S]+?)(?=\n\[|\Z)")
_URL_KEYS = ("url", "fileUrl", "signedUrl", "publicUrl", "fullPath")

# 모델 API 의 이미지 한 장 한도(OpenAI 20MB)를 넘기지 않는다.
MAX_IMAGE_BYTES = 20 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 20.0


def _image_ref(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in _URL_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def split_input_images(text: str) -> tuple[str, list[str]]:
    """본문의 `[InputData].images` 를 떼어 내고 (남은 본문, 이미지 주소 목록) 을 돌려준다.

    이미지가 없거나 블록을 읽지 못하면 본문을 그대로 두고 빈 목록을 돌려준다.
    다른 항목(파일 등)은 `[InputData]` 에 그대로 남긴다.
    """
    if not isinstance(text, str) or "[InputData]" not in text:
        return text, []
    m = _INPUT_DATA_RE.search(text)
    if not m:
        return text, []
    try:
        data = json.loads(m.group(1).strip())
    except (ValueError, TypeError):
        return text, []
    if not isinstance(data, dict) or not isinstance(data.get("images"), list):
        return text, []

    refs = [r for r in (_image_ref(i) for i in data.pop("images")) if r]
    if not refs:
        return text, []

    rest = f"[InputData]\n{json.dumps(data, ensure_ascii=False)}" if data else ""
    head = text[: m.start()].rstrip()
    tail = text[m.end():].strip("\n")  # 뒤따르는 [OpenUIForm] 같은 블록
    return "\n\n".join(p for p in (head, rest, tail) if p), refs


async def _download_as_data_url(url: str, client: Any) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    mime = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        raise ValueError(f"이미지가 아닌 응답: {mime or '(없음)'}")
    body = resp.content
    if len(body) > MAX_IMAGE_BYTES:
        raise ValueError(f"이미지가 너무 큼: {len(body)} bytes")
    return f"data:{mime};base64,{base64.b64encode(body).decode('ascii')}"


async def resolve_images(refs: list[str]) -> tuple[list[str], int]:
    """이미지 주소를 모델에 바로 넣을 data URL 로 바꾼다. (성공 목록, 실패 수) 를 돌려준다."""
    resolved: list[str] = []
    failed = 0
    remote = [r for r in refs if not r.lower().startswith("data:")]
    client = None
    try:
        if remote:
            import httpx

            client = httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True)
        for ref in refs:
            if ref.lower().startswith("data:"):
                if ref.lower().startswith("data:image/"):
                    resolved.append(ref)
                else:
                    failed += 1
                continue
            if not re.match(r"^https?://", ref, re.I):
                failed += 1
                continue
            try:
                resolved.append(await _download_as_data_url(ref, client))
            except Exception as exc:  # 한 장을 못 받았다고 턴 전체를 멈추지 않는다
                failed += 1
                logger.warning("첨부 이미지 내려받기 실패 | url=%s err=%s", ref.split("?")[0], exc)
    finally:
        if client is not None:
            await client.aclose()
    return resolved, failed


def images_note(count: int, failed: int) -> str:
    """본문에 붙일 안내. 모델이 이미지가 있다는 것과 못 받은 장수를 알게 한다."""
    parts = []
    if count:
        parts.append(f"사용자가 이미지 {count}장을 첨부했습니다. 이 메시지에 이미지로 함께 실었으니 직접 보고 내용을 읽으세요.")
    if failed:
        parts.append(f"첨부 이미지 {failed}장은 불러오지 못했습니다. 그 사실을 사용자에게 알리세요.")
    return f"[첨부 이미지] {' '.join(parts)}" if parts else ""


def user_content(text: str, images: list[str] | None) -> Any:
    """사용자 메시지 내용. 이미지가 있으면 글 블록 뒤에 `image_url` 블록을 붙인다."""
    if not images:
        return text
    return [
        {"type": "text", "text": text},
        *({"type": "image_url", "image_url": {"url": url}} for url in images),
    ]
