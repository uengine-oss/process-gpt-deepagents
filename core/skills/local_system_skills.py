"""core/skills/local_system_skills.py — 오프라인 안전 내장 시스템 스킬 시딩.

process-gpt가 자체 제공하는 "시스템 스킬"(테넌트 구분 없이 모든 테넌트가
공통으로 써야 하는 스킬)을 저장소에 직접 번들링해 Docker 이미지에 포함시키고,
매 실행 시작 시 `{SKILLS_DIRS}/process-gpt-system/`으로 동기화한다.

`git_skill_fetcher.prefetch_git_skills()`와 달리 네트워크 접속이 전혀 필요
없다 — 인터넷이 차단된 폐쇄망/설치형(on-prem) 환경에서도 항상 동작한다.
원본은 레포 루트의 `skills/`(각 시스템 스킬이 git submodule)에 있으며,
deepagents의 Dockerfile이 (빌드 컨텍스트를 레포 루트로 잡고) 이를
`/app/system-skills/process-gpt-system/`으로 COPY해 이미지에 함께 구워진다.
`SKILLS_DIRS`(기본 `skills/`, 컨테이너 `/app/skills`)는 배포 시 host volume으로
덮어써질 수 있으므로, 원본을 별도 경로(`system-skills/`)에 두어 그 마운트에
가려지지 않게 한다.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent.parent.parent

SYSTEM_COLLECTION_NAME = "process-gpt-system"

_SOURCE_DIR = _BASE_DIR / "system-skills" / SYSTEM_COLLECTION_NAME


def _resolve_skills_root() -> Path:
    raw = os.getenv("SKILLS_DIRS", str(_BASE_DIR / "skills"))
    first = raw.split(",")[0].strip() or str(_BASE_DIR / "skills")
    return Path(first) if os.path.isabs(first) else (_BASE_DIR / first).resolve()


def seed_local_system_skills() -> int:
    """번들 시스템 스킬을 `{SKILLS_DIRS}/process-gpt-system/`으로 동기화한다.

    이미지에 구워진 원본(`_SOURCE_DIR`)이 항상 source of truth다 — 대상
    디렉토리의 동일 스킬은 매번 덮어써 재기동만으로 최신 버전이 반영되게
    한다. 테넌트별 업로드 스킬(`{tenant_id}/local/`)과는 경로가 겹치지
    않으므로 테넌트 데이터에는 영향이 없다.

    Returns
    -------
    int
        동기화된 스킬 개수. 원본 디렉토리가 없으면 0.
    """
    if not _SOURCE_DIR.is_dir():
        return 0

    dest_root = _resolve_skills_root() / SYSTEM_COLLECTION_NAME
    synced = 0
    for entry in sorted(_SOURCE_DIR.iterdir()):
        if not entry.is_dir() or not (entry / "SKILL.md").exists():
            continue
        dest = dest_root / entry.name
        try:
            if dest.exists():
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(entry, dest)
            synced += 1
        except OSError:
            logger.exception("local-system-skills: %s 동기화 실패", entry.name)
    return synced
