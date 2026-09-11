"""core/sandbox/tenant_workspace.py — 테넌트별 작업공간 경로.

샌드박스 컨테이너는 테넌트 단위로 재사용되고, 에이전트는 그 안에서 임의 셸을 실행한다.
도구 레벨 경로 검사로는 셸을 막을 수 없으므로 격리는 마운트로 한다 — 컨테이너에는 자기
테넌트 디렉터리만 `/workspace` 로 들어간다. 호스트에서 도는 도구도 같은 디렉터리를 기준으로
경로를 풀어야, 컨테이너에서 보이지 않는 파일을 호스트 도구로 우회해 열 수 없다.
"""

from __future__ import annotations

import re
from pathlib import Path

# 디렉터리 이름으로 그대로 쓴다. 치환하지 않고 거부한다 — 치환하면 서로 다른 테넌트가 같은
# 디렉터리로 합쳐질 수 있다. 첫 글자 제한으로 `.`/`..` 도 걸러진다.
_TENANT_DIR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def tenant_dir_name(tenant_id: str) -> str:
    value = str(tenant_id or "")
    if not _TENANT_DIR.fullmatch(value):
        raise ValueError(f"작업공간을 만들 수 없는 tenant_id 입니다: {tenant_id!r}")
    return value


def tenant_workspace(root: Path | str, tenant_id: str) -> Path:
    """볼륨 루트 아래 이 테넌트의 작업공간. 테넌트가 없으면 공용 루트로 떨어지지 않고 실패한다."""
    return Path(root) / tenant_dir_name(tenant_id)


def ensure_inside(base: Path, candidate: Path) -> None:
    """`..`·심볼릭 링크를 풀었을 때 candidate 가 base 밖이면 ValueError."""
    try:
        candidate.resolve().relative_to(base.resolve())
    except ValueError:
        raise ValueError(f"작업공간 밖 경로입니다: {candidate}") from None
