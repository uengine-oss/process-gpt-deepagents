"""샌드박스의 테넌트 스킬 마운트.

배경: 샌드박스 컨테이너는 테넌트당 하나를 띄워 계속 재사용한다. 스킬 디렉토리는 띄울 때 있던
것만 /skills/<name> 으로 마운트하는데, 테넌트가 아직 스킬을 올리지 않아 {skills_host}/{tenant}
가 없을 때 띄운 컨테이너는 그 뒤로도 /skills/<tenant> 가 없다. 그래서 「스킬 추가」로 올리고
에이전트에 붙인 스킬을 SkillsMiddleware 가 「path_not_found」로 읽지 못했다(튜토리얼 VM,
``FilteredSkillsMiddleware: allowed=['hanbit-quote-format'] loaded=[]``).
여기서는 (1) 테넌트 디렉토리를 미리 만들어 늘 마운트하는지, (2) 그 마운트가 없는 채로 떠 있는
컨테이너를 찾아내는지 본다.
"""

from __future__ import annotations

from core.sandbox import docker_sandbox as ds


class _FakeContainer:
    def __init__(self, destinations):
        self.attrs = {"Mounts": [{"Destination": d} for d in destinations]}

    def reload(self):
        pass


def _sandbox(tmp_path, monkeypatch, tenant="acme"):
    skills = tmp_path / "skills"
    (skills / "anthropics-skills").mkdir(parents=True)
    (skills / "other-tenant").mkdir()
    monkeypatch.setattr("core.skills.skills._global_collection_names", lambda: {"anthropics-skills"})
    return ds.DockerSandboxBackend(tenant_id=tenant, workspace_host=tmp_path / "ws", skills_host=skills), skills


def test_tenant_skill_dir_is_created_and_mounted_before_any_upload(tmp_path, monkeypatch):
    sb, skills = _sandbox(tmp_path, monkeypatch)
    binds = {v["bind"] for v in sb._skill_volumes().values()}
    assert (skills / "acme").is_dir()
    assert binds == {"/skills/anthropics-skills", "/skills/acme"}  # 다른 테넌트 디렉토리는 붙이지 않는다


def test_container_without_tenant_skill_mount_is_reported(tmp_path, monkeypatch):
    sb, _ = _sandbox(tmp_path, monkeypatch)
    old = _FakeContainer(["/workspace", "/skills/anthropics-skills"])
    assert sb._missing_skill_mounts(old) == ["/skills/acme"]


def test_container_with_all_skill_mounts_is_reused(tmp_path, monkeypatch):
    sb, _ = _sandbox(tmp_path, monkeypatch)
    ok = _FakeContainer(["/workspace", "/skills/anthropics-skills", "/skills/acme"])
    assert sb._missing_skill_mounts(ok) == []
