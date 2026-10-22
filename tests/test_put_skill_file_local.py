"""PUT /skills/{name}/files/{path} — git 없이 스킬 파일을 직접 저장한다.

Git 공급자가 연결되지 않은 테넌트도 스킬을 편집·저장할 수 있어야 한다(버전·병합 관리는
git 이 연결된 경우에만 제공). 기존 저장 경로(`/skills/{name}/commit`)는 provider 를
요구해 git 미연결 테넌트에서 항상 실패했다.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from core.api.skills_router import routes

# _find_skill_dir 는 호출 시점에 core.skills.skills 를 import 한다. 같은 실행에서 앞서 도는
# test_input_data_offload.py 가 sys.path 를 오염시켜(langchain_core/a2a 가 패키지가 아니게 됨)
# 그 지연 import 를 깨뜨리므로, 수집 시점에 미리 import 해 sys.modules 에 올려 둔다.
import core.skills.skills  # noqa: F401  (import 부작용이 목적)

TENANT = "tenant1"
SKILL = "my-skill"


@pytest.fixture
def client():
    return TestClient(Starlette(routes=routes), raise_server_exceptions=True)


@pytest.fixture
def skill_dir(tmp_path, monkeypatch) -> Path:
    """SKILLS_DIRS 를 격리하고 <root>/<tenant>/local/<skill>/SKILL.md 를 만든다."""
    monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))
    d = tmp_path / TENANT / "local" / SKILL
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: my-skill\ndescription: d\n---\n\nv1\n", encoding="utf-8")
    return d


def _put(client, file_path, content, skill=SKILL):
    # git provider 미연결 상태를 모사한다.
    with patch("core.api.skills_router._get_provider", side_effect=ValueError("no git")):
        return client.put(
            f"/skills/{skill}/files/{file_path}",
            json={"content": content, "tenant_id": TENANT},
        )


def test_writes_existing_file_without_git(client, skill_dir):
    resp = _put(client, "SKILL.md", "---\nname: my-skill\ndescription: d\n---\n\nv2\n")
    assert resp.status_code == 200, resp.text
    assert resp.json()["saved"] is True
    assert "v2" in (skill_dir / "SKILL.md").read_text()


def test_creates_nested_file(client, skill_dir):
    resp = _put(client, "references/checklist.md", "체크리스트")
    assert resp.status_code == 200, resp.text
    assert (skill_dir / "references" / "checklist.md").read_text() == "체크리스트"


def test_rejects_path_traversal(client, skill_dir):
    # 평문 '../..' 는 HTTP 클라이언트가 URL 을 정규화해 다른 라우트로 가버리므로,
    # 핸들러의 가드를 실제로 타도록 퍼센트 인코딩한 경로로 요청한다.
    resp = _put(client, "..%2F..%2Fescaped.md", "nope")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid file_path"
    assert not (skill_dir.parent.parent / "escaped.md").exists()


def test_requires_content_string(client, skill_dir):
    with patch("core.api.skills_router._get_provider", side_effect=ValueError("no git")):
        resp = client.put(f"/skills/{SKILL}/files/SKILL.md", json={"tenant_id": TENANT})
    assert resp.status_code == 400
    assert "content" in resp.json()["error"]


def test_unknown_skill_returns_404(client, skill_dir):
    resp = _put(client, "SKILL.md", "x", skill="does-not-exist")
    assert resp.status_code == 404


def test_does_not_shadow_get_or_delete(client, skill_dir):
    """같은 경로에 GET/DELETE 가 이미 있으므로 PUT 추가가 그들을 가리지 않아야 한다."""
    with patch("core.api.skills_router._get_provider", side_effect=ValueError("no git")):
        resp = client.get(f"/skills/{SKILL}/files/SKILL.md?tenant_id={TENANT}")
    assert resp.status_code == 200, resp.text
    assert "v1" in resp.json().get("content", "")


@pytest.fixture
def system_skill_dir(tmp_path, monkeypatch) -> Path:
    """서비스 기본 스킬 컬렉션(<root>/process-gpt-system/<skill>)."""
    monkeypatch.setenv("SKILLS_DIRS", str(tmp_path))
    d = tmp_path / "process-gpt-system" / "bsc-strategy-interview"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: bsc-strategy-interview\ndescription: d\n---\n\nv1\n", encoding="utf-8")
    return d


def test_system_skill_is_not_written(client, system_skill_dir):
    """서비스 기본 스킬은 모든 테넌트가 함께 쓰고 재시작하면 원본으로 덮어써진다 — 화면에서 고칠 수 없다."""
    resp = _put(client, "SKILL.md", "---\nname: bsc-strategy-interview\ndescription: d\nmetadata:\n  include_tools: x\n---\n", skill="bsc-strategy-interview")
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "system_skill_read_only"
    assert "v1" in (system_skill_dir / "SKILL.md").read_text()


def test_file_list_tells_whether_skill_is_a_system_skill(client, skill_dir, system_skill_dir):
    with patch("core.api.skills_router._get_provider", side_effect=ValueError("no git")):
        system = client.get(f"/skills/bsc-strategy-interview/files?tenant_id={TENANT}").json()
        own = client.get(f"/skills/{SKILL}/files?tenant_id={TENANT}").json()
    assert system["system"] is True
    assert own["system"] is False
