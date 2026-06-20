"""core/sandbox/docker_sandbox.py — Docker 기반 격리 샌드박스 백엔드.

BaseSandbox를 상속해 execute()/upload_files()/download_files()를 구현한다.
tenant_id 당 하나의 컨테이너를 재사용하며, atexit 훅으로 프로세스 종료 시 정리한다.

컨테이너는 다음 볼륨을 마운트한다:
  - {workspace_host}/  →  /workspace  (read-write)
  - {skills_host}/     →  /skills     (read-only, optional)

환경변수는 호스트 전체를 상속하지 않고, 스킬 후처리(저장/검증)에 필요한
**화이트리스트(_FORWARD_ENV_KEYS)만** 컨테이너로 전달한다. 또한 요청이 들어온
tenant 로 `TENANT_ID` 를 자동 세팅하고, 로컬(127.0.0.1/localhost) Supabase·완료엔진
주소는 컨테이너에서 닿도록 host.docker.internal 로 치환한다.
"""

from __future__ import annotations

import atexit
import io
import logging
import os
import re
import tarfile
import threading
from pathlib import Path, PurePosixPath

import docker
import docker.errors
from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

logger = logging.getLogger(__name__)

SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "python:3.13-slim")
_CONTAINER_WORKSPACE = "/workspace"
_MAX_OUTPUT_BYTES = 100_000

# 컨테이너로 전달할 호스트 env 화이트리스트.
# bpmn-process-generation-skill 후처리(scripts/run_postprocess.py)의 저장/검증에 필요.
_FORWARD_ENV_KEYS = (
    # Supabase (core/db.py 와 동일 변수명)
    "SUPABASE_URL", "SUPABASE_KEY_URL", "SERVICE_ROLE_KEY", "SUPABASE_KEY", "SUPABASE_ANON_KEY",
    # LLM (core/model.py 와 동일 변수명) — 검증 자동교정용
    "LLM_MODEL", "LLM_PROXY_URL", "LLM_PROXY_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    # 실행 검증 엔진 + 튜닝
    "COMPLETION_ENGINE_URL",
    "PDF2BPMN_VALIDATION_MAX_ITERS",
    "PDF2BPMN_VALIDATION_ADVANCE_TIMEOUT",
    "PDF2BPMN_VALIDATION_CLEANUP",
)

_HOST_GATEWAY = "host.docker.internal"
# 컨테이너 내부에서 호스트 로컬 서비스에 닿게 하려면 loopback 주소를 치환해야 한다.
_LOOPBACK_URL_KEYS = ("SUPABASE_URL", "SUPABASE_KEY_URL", "COMPLETION_ENGINE_URL")


def _rewrite_loopback(url: str) -> str:
    if not url:
        return url
    return url.replace("127.0.0.1", _HOST_GATEWAY).replace("localhost", _HOST_GATEWAY)


def _build_container_env(tenant_id: str) -> dict[str, str]:
    """화이트리스트 host env + 요청 tenant 자동주입(TENANT_ID) + loopback 치환."""
    env: dict[str, str] = {}
    for k in _FORWARD_ENV_KEYS:
        v = os.environ.get(k)
        if v:
            env[k] = v
    for url_key in _LOOPBACK_URL_KEYS:
        if env.get(url_key):
            env[url_key] = _rewrite_loopback(env[url_key])
    # 요청이 들어온 tenant 로 자동 세팅 (스킬 scripts 가 --tenant 없이 TENANT_ID 로 읽는다)
    env["TENANT_ID"] = tenant_id or ""
    return env

# "bash: line 1: curl: command not found" 또는 "sh: jq: not found" 에서 커맨드명 추출
_CMD_NOT_FOUND_RE = re.compile(
    r"(?:bash|sh|zsh|dash)(?:: line \d+)?: (\S+): (?:command not found|not found)"
)

# 커맨드명과 apt 패키지명이 다른 경우
_PACKAGE_OVERRIDES: dict[str, str] = {
    "python": "python3",
    "pip": "python3-pip",
    "pip3": "python3-pip",
    "node": "nodejs",
}

_registry: dict[str, "DockerSandboxBackend"] = {}
_registry_lock = threading.Lock()

_FALLBACK_SOCKETS = [
    "/var/run/docker.sock",
    os.path.expanduser("~/.colima/default/docker.sock"),
    os.path.expanduser("~/.docker/run/docker.sock"),
]


def _get_docker_client() -> docker.DockerClient:
    """DOCKER_HOST가 없으면 알려진 소켓 경로를 순서대로 시도한다."""
    if os.environ.get("DOCKER_HOST"):
        return docker.from_env()
    for sock in _FALLBACK_SOCKETS:
        if os.path.exists(sock):
            return docker.DockerClient(base_url=f"unix://{sock}")
    return docker.from_env()


class DockerSandboxBackend(BaseSandbox):
    """tenant_id 당 하나의 Docker 컨테이너를 재사용하는 샌드박스 백엔드."""

    def __init__(
        self,
        tenant_id: str,
        workspace_host: Path,
        skills_host: Path | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._workspace_host = workspace_host
        self._skills_host = skills_host
        self._container: docker.models.containers.Container | None = None
        self._lock = threading.Lock()
        self._client: docker.DockerClient | None = None
        # bpmn-process-generation-skill 저장(run_postprocess)에 필요한 supabase 패키지를
        # 샌드박스(python:3.13-slim)에 1회 보장 설치했는지 플래그(프로세스 단위).
        self._deps_ensured = False

    # ------------------------------------------------------------------
    # 컨테이너 생명주기
    # ------------------------------------------------------------------

    def _ensure_container(self) -> docker.models.containers.Container:
        with self._lock:
            if self._client is None:
                self._client = _get_docker_client()
            # 기존 컨테이너가 살아있으면 재사용
            if self._container is not None:
                try:
                    self._container.reload()
                    if self._container.status == "running":
                        return self._container
                except docker.errors.NotFound:
                    pass
                self._container = None

            self._container = self._start_container()
            return self._container

    def _start_container(self) -> docker.models.containers.Container:
        name = f"deepagent-sandbox-{self._tenant_id or 'shared'}"

        # 이전 실행에서 남은 중단된 컨테이너 정리
        try:
            old = self._client.containers.get(name)
            if old.status == "running":
                self._container = old
                return old
            old.remove(force=True)
        except docker.errors.NotFound:
            pass

        self._workspace_host.mkdir(parents=True, exist_ok=True)
        volumes: dict = {str(self._workspace_host): {"bind": _CONTAINER_WORKSPACE, "mode": "rw"}}
        if self._skills_host and self._skills_host.exists():
            volumes[str(self._skills_host)] = {"bind": "/skills", "mode": "ro"}

        container = self._client.containers.run(
            SANDBOX_IMAGE,
            command=["sleep", "infinity"],
            name=name,
            detach=True,
            auto_remove=True,
            volumes=volumes,
            working_dir=_CONTAINER_WORKSPACE,
            environment=_build_container_env(self._tenant_id),
            extra_hosts={_HOST_GATEWAY: "host-gateway"},
            mem_limit="512m",
            cpu_period=100_000,
            cpu_quota=50_000,  # 50% of one CPU core
        )
        logger.info("sandbox started: %s (tenant=%s)", name, self._tenant_id)
        return container

    def cleanup(self) -> None:
        with self._lock:
            if self._container is not None:
                try:
                    self._container.stop(timeout=5)
                except Exception:
                    pass
                self._container = None
                logger.info("sandbox stopped (tenant=%s)", self._tenant_id)

    # ------------------------------------------------------------------
    # BaseSandbox 구현
    # ------------------------------------------------------------------

    @property
    def id(self) -> str:
        return f"docker-{self._tenant_id or 'shared'}"

    def _ensure_deps(self, container: "docker.models.containers.Container") -> None:
        """저장 스크립트(run_postprocess)가 쓰는 supabase/httpx 를 샌드박스에 1회 보장 설치.

        기본 이미지(python:3.13-slim)에는 supabase 가 없어, 모델이 pip install 을 빠뜨리면
        저장이 'supabase 패키지 없음' 으로 실패한다. import 가드로 이미 있으면 즉시 통과(빠름),
        없으면 설치한다(최초 1회 ~10여초). 프로세스 단위로 1회만 시도한다.
        """
        if self._deps_ensured:
            return
        try:
            check = "python -c 'import supabase' 2>/dev/null || pip install -q supabase httpx"
            container.exec_run(["bash", "-c", check], demux=False)
        except Exception:
            logger.warning("sandbox 의존성(supabase) 보장 설치 실패 — 저장 시 재시도 필요", exc_info=True)
        self._deps_ensured = True

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        container = self._ensure_container()
        self._ensure_deps(container)
        _timeout = timeout or 180

        def _exec(cmd: str) -> tuple[int, str]:
            holder: dict = {}

            def _run() -> None:
                # Don't pass workdir= to exec_run: on macOS + Colima/Docker Desktop,
                # runc resolves the bind-mount target back to the host path and
                # raises "current working directory is outside of container mount
                # namespace root". cd inside bash bypasses that OCI-level check.
                #
                # 서브셸 여는/닫는 괄호는 반드시 **각자 자기 줄**에 둔다. cmd 와 같은 줄에
                # ` )` 를 붙이면, cmd 가 heredoc(<<'JSON' ... JSON)으로 끝날 때 닫는 구분자
                # 줄이 "JSON )" 로 오염돼 heredoc 이 깨진다(bash: here-document ... wanted `JSON').
                wrapped = f"cd {_CONTAINER_WORKSPACE} && (\n{cmd}\n)"
                exit_code, output = container.exec_run(
                    ["bash", "-c", wrapped],
                    demux=False,
                )
                holder["exit_code"] = exit_code
                holder["output"] = (output or b"").decode("utf-8", errors="replace")

            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            thread.join(_timeout)

            if thread.is_alive():
                return 124, f"ERROR: command timed out after {_timeout}s"
            return holder.get("exit_code") or 0, holder.get("output", "")

        exit_code, out = _exec(command)

        # 커맨드를 찾을 수 없는 경우 apt-get으로 자동 설치 후 1회 재시도
        match = _CMD_NOT_FOUND_RE.search(out)
        if match:
            missing = match.group(1)
            pkg = _PACKAGE_OVERRIDES.get(missing, missing)
            logger.info("auto-installing missing command %r (package=%r)", missing, pkg)
            install_cmd = (
                "DEBIAN_FRONTEND=noninteractive apt-get update -qq 2>&1 | tail -1 "
                f"&& apt-get install -y -qq --no-install-recommends {pkg} 2>&1 | tail -3"
            )
            container.exec_run(["bash", "-c", install_cmd], demux=False)
            exit_code, out = _exec(command)

        truncated = len(out) > _MAX_OUTPUT_BYTES
        if truncated:
            out = out[:_MAX_OUTPUT_BYTES] + "\n...[truncated]"
        return ExecuteResponse(
            output=out,
            exit_code=exit_code,
            truncated=truncated,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        container = self._ensure_container()
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                # 컨테이너는 항상 Linux 이므로 POSIX 경로(슬래시)로 다룬다.
                # (Windows 호스트에서 Path(...) 를 쓰면 '\workspace' 처럼 백슬래시가 되어
                #  put_archive 가 404 'Could not find \workspace' 로 실패한다.)
                posix = PurePosixPath(path)
                parent = str(posix.parent)
                container.exec_run(["mkdir", "-p", parent])
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w") as tar:
                    info = tarfile.TarInfo(name=posix.name)
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
                buf.seek(0)
                container.put_archive(parent, buf)
                responses.append(FileUploadResponse(path=path))
            except Exception as exc:
                responses.append(FileUploadResponse(path=path, error=str(exc)))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        container = self._ensure_container()
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                stream, _ = container.get_archive(path)
                buf = io.BytesIO(b"".join(stream))
                buf.seek(0)
                with tarfile.open(fileobj=buf) as tar:
                    members = tar.getmembers()
                    if not members:
                        responses.append(FileDownloadResponse(path=path, error="file_not_found"))
                        continue
                    f = tar.extractfile(members[0])
                    content = f.read() if f else b""
                responses.append(FileDownloadResponse(path=path, content=content))
            except docker.errors.NotFound:
                responses.append(FileDownloadResponse(path=path, error="file_not_found"))
            except Exception as exc:
                responses.append(FileDownloadResponse(path=path, error=str(exc)))
        return responses


# ------------------------------------------------------------------
# 전역 레지스트리 / 팩토리
# ------------------------------------------------------------------

def get_or_create_sandbox(
    tenant_id: str,
    workspace_host: Path,
    skills_host: Path | None = None,
) -> DockerSandboxBackend:
    """tenant_id에 대응하는 샌드박스 백엔드를 반환한다. 없으면 새로 생성한다."""
    key = tenant_id or "shared"
    with _registry_lock:
        if key not in _registry:
            _registry[key] = DockerSandboxBackend(tenant_id, workspace_host, skills_host)
    return _registry[key]


def cleanup_sandbox(tenant_id: str) -> None:
    """특정 tenant의 샌드박스 컨테이너를 중지하고 레지스트리에서 제거한다."""
    key = tenant_id or "shared"
    with _registry_lock:
        backend = _registry.pop(key, None)
    if backend:
        backend.cleanup()


@atexit.register
def _cleanup_all() -> None:
    with _registry_lock:
        backends = list(_registry.values())
        _registry.clear()
    for backend in backends:
        backend.cleanup()
