"""테넌트 작업공간 격리와 샌드박스 환경변수를 검증한다.

배경: 모든 테넌트의 샌드박스가 작업공간 볼륨 전체를 /workspace 로 마운트하고 모델 API 키를
환경변수로 받았다. 에이전트는 그 안에서 임의 셸을 실행하므로, 사용자가 채팅으로 `env` 나
`ls /workspace/.bpmn` 을 시켜 키와 다른 테넌트의 작업 파일을 그대로 볼 수 있었다.
"""

from __future__ import annotations

import docker.errors
import pytest

from core.sandbox import docker_sandbox as ds
from core.skills import tools as skill_tools
from core.storage import storage


# ---------------------------------------------------------------------------
# 샌드박스 컨테이너
# ---------------------------------------------------------------------------

class _FakeContainer:
    def __init__(self, labels=None, status="running"):
        self.status = status
        self.attrs = {"Config": {"Labels": dict(labels or {})}}
        self.removed = False

    def reload(self):
        pass

    def remove(self, force=False):
        self.removed = True


class _FakeContainers:
    def __init__(self, existing=None):
        self.existing = existing
        self.run_kwargs = None
        self.created = None

    def get(self, name):
        if self.existing is None:
            raise docker.errors.NotFound("no container")
        return self.existing

    def run(self, image, **kwargs):
        self.run_kwargs = kwargs
        self.created = _FakeContainer(labels=kwargs.get("labels"))
        return self.created


class _FakeClient:
    def __init__(self, existing=None):
        self.containers = _FakeContainers(existing)


@pytest.fixture
def make_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "SANDBOX_NETWORK_MTU", 0)
    monkeypatch.setattr(ds.DockerSandboxBackend, "_configure_git", staticmethod(lambda container: None))
    monkeypatch.setattr(ds.DockerSandboxBackend, "_bootstrap_tooling", lambda self, container: None)

    def make(tenant_id, existing=None):
        backend = ds.DockerSandboxBackend(tenant_id, workspace_host=tmp_path / "workspace")
        backend._client = _FakeClient(existing)
        return backend

    return make


def test_container_env_carries_no_model_keys(monkeypatch):
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_PROXY_API_KEY", "LLM_PROXY_URL", "LLM_MODEL"):
        monkeypatch.setenv(key, "secret-value")

    assert ds._build_container_env("acme") == {"TENANT_ID": "acme"}


def test_container_mounts_only_its_tenant_workspace(make_backend, tmp_path):
    backend = make_backend("acme")
    backend._start_container()

    kwargs = backend._client.containers.run_kwargs
    workspace_mounts = {
        source: spec for source, spec in kwargs["volumes"].items() if spec["bind"] == "/workspace"
    }
    assert workspace_mounts == {
        str(tmp_path / "workspace" / "acme"): {"bind": "/workspace", "mode": "rw"}
    }
    assert kwargs["environment"] == {"TENANT_ID": "acme"}
    assert kwargs["labels"] == {ds._CONTRACT_LABEL: ds.SANDBOX_CONTRACT}


def test_container_started_before_isolation_is_replaced(make_backend):
    # 배포 전부터 떠 있던 컨테이너(볼륨 전체 마운트·모델 키 env)는 이름이 같아도 재사용하지 않는다.
    old = _FakeContainer(labels={})
    backend = make_backend("acme", existing=old)

    container = backend._start_container()

    assert old.removed
    assert container is backend._client.containers.created


def test_container_with_current_contract_is_reused(make_backend):
    current = _FakeContainer(labels={ds._CONTRACT_LABEL: ds.SANDBOX_CONTRACT})
    backend = make_backend("acme", existing=current)

    assert backend._start_container() is current
    assert not current.removed
    assert backend._client.containers.run_kwargs is None


@pytest.mark.parametrize("tenant_id", ["", ".", "..", "../globex", "acme/globex", " acme"])
def test_sandbox_refuses_tenant_that_is_not_a_directory_name(tmp_path, tenant_id):
    with pytest.raises(ValueError):
        ds.DockerSandboxBackend(tenant_id, workspace_host=tmp_path)


# ---------------------------------------------------------------------------
# 호스트에서 도는 도구
# ---------------------------------------------------------------------------

@pytest.fixture
def use_tenant(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    monkeypatch.setattr(skill_tools, "WORKSPACE_ROOT", root)
    tokens = []

    def use(tenant_id):
        tokens.append(skill_tools._current_tenant_id.set(tenant_id))
        return root / tenant_id

    yield use
    for token in reversed(tokens):
        skill_tools._current_tenant_id.reset(token)


def test_container_paths_resolve_into_the_current_tenant(use_tenant):
    acme = use_tenant("acme")

    assert skill_tools._resolve_ws_path("/workspace/.bpmn/room/a.json") == acme / ".bpmn" / "room" / "a.json"
    assert skill_tools._resolve_ws_path("{ws}/uploads/a.docx") == acme / "uploads" / "a.docx"


def test_tenants_get_separate_workspaces(use_tenant):
    acme = use_tenant("acme")
    assert skill_tools.tenant_workspace_root() == acme

    globex = use_tenant("globex")
    assert skill_tools.tenant_workspace_root() == globex
    assert acme != globex


def test_parent_segments_cannot_reach_another_tenant(use_tenant):
    use_tenant("acme")

    with pytest.raises(ValueError):
        skill_tools._resolve_ws_path("/workspace/../globex/.bpmn/room/secret.md")


def test_host_path_of_a_tenant_sharing_the_prefix_is_rejected(use_tenant):
    acme = use_tenant("acme")
    sibling = acme.parent / "acme2" / "secret.md"

    with pytest.raises(ValueError):
        skill_tools._resolve_ws_path(str(sibling))


def test_workspace_paths_fail_without_a_tenant(use_tenant):
    use_tenant("")

    with pytest.raises(ValueError):
        skill_tools.tenant_workspace_root()


def test_storage_tools_only_touch_the_current_tenant_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "WORKSPACE_ROOT", tmp_path)
    token = storage._current_tenant_id.set("acme")
    try:
        storage._validate_workspace_path(tmp_path / "acme" / "docs", "dest_dir")
        with pytest.raises(ValueError):
            storage._validate_workspace_path(tmp_path / "globex" / "docs", "dest_dir")
    finally:
        storage._current_tenant_id.reset(token)
