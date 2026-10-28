"""샌드박스 bind-mount 원본을 데몬 쪽 경로로 바꾸는지 (deepagents 가 컨테이너로 돌 때)."""
from types import SimpleNamespace

from core.sandbox import docker_sandbox as sb


class _FakeClient:
    def __init__(self, mounts):
        self.containers = SimpleNamespace(get=lambda _name: SimpleNamespace(attrs={"Mounts": mounts}))


def _reset(monkeypatch, in_container: bool):
    monkeypatch.setattr(sb, "_DAEMON_MOUNTS", None)
    real_exists = sb.os.path.exists
    monkeypatch.setattr(sb.os.path, "exists", lambda p: in_container if p == "/.dockerenv" else real_exists(p))


def test_named_volume_path_is_translated(monkeypatch):
    _reset(monkeypatch, in_container=True)
    client = _FakeClient([
        {"Type": "volume", "Source": "/var/lib/docker/volumes/pgpt_deepagents-workspace/_data", "Destination": "/app/workspace"},
        {"Type": "volume", "Source": "/var/lib/docker/volumes/pgpt_skills-storage/_data", "Destination": "/app/skills"},
        {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock"},
    ])
    assert sb._daemon_path(client, "/app/workspace/localhost") == "/var/lib/docker/volumes/pgpt_deepagents-workspace/_data/localhost"
    assert sb._daemon_path(client, "/app/skills/process-gpt-system") == "/var/lib/docker/volumes/pgpt_skills-storage/_data/process-gpt-system"
    # 접두사만 같은 다른 경로는 바꾸지 않는다
    assert sb._daemon_path(client, "/app/workspace2/x") == "/app/workspace2/x"


def test_outside_container_paths_are_unchanged(monkeypatch):
    _reset(monkeypatch, in_container=False)
    client = _FakeClient([{"Source": "/somewhere", "Destination": "/app/workspace"}])
    assert sb._daemon_path(client, "/app/workspace/localhost") == "/app/workspace/localhost"
