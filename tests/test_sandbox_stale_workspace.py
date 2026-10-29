"""다시 설치한 뒤 남은 샌드박스(지워진 작업 볼륨을 문 컨테이너)는 재사용하지 않고 다시 띄운다."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.sandbox import docker_sandbox as ds


def _container(exit_code=0, raises=False):
    c = MagicMock()
    if raises:
        c.exec_run.side_effect = RuntimeError("container gone")
    else:
        c.exec_run.return_value = SimpleNamespace(exit_code=exit_code, output=b"")
    return c


def test_workspace_alive_when_cd_succeeds():
    assert ds.DockerSandboxBackend._workspace_alive(_container(0)) is True


def test_workspace_dead_when_cd_fails():
    # 지워진 볼륨: OCI runtime 이 126 등으로 실패한다
    assert ds.DockerSandboxBackend._workspace_alive(_container(126)) is False


def test_workspace_dead_when_exec_raises():
    assert ds.DockerSandboxBackend._workspace_alive(_container(raises=True)) is False
