"""core/sandbox/docker_sandbox.py — Docker 기반 격리 샌드박스 백엔드.

BaseSandbox를 상속해 execute()/upload_files()/download_files()를 구현한다.
tenant_id 당 하나의 컨테이너를 재사용하며, atexit 훅으로 프로세스 종료 시 정리한다.

컨테이너는 다음 볼륨을 마운트한다:
  - {workspace_host}/  →  /workspace  (read-write)
  - {skills_host}/     →  /skills     (read-only, optional)

환경변수는 호스트에서 상속하지 않는다.
"""

from __future__ import annotations

import atexit
import io
import logging
import os
import re
import tarfile
import threading
from pathlib import Path

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

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        container = self._ensure_container()
        _timeout = timeout or 180

        def _exec(cmd: str) -> tuple[int, str]:
            holder: dict = {}

            def _run() -> None:
                # Don't pass workdir= to exec_run: on macOS + Colima/Docker Desktop,
                # runc resolves the bind-mount target back to the host path and
                # raises "current working directory is outside of container mount
                # namespace root". cd inside bash bypasses that OCI-level check.
                wrapped = f"cd {_CONTAINER_WORKSPACE} && ( {cmd} )"
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
                parent = str(Path(path).parent)
                container.exec_run(["mkdir", "-p", parent])
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w") as tar:
                    info = tarfile.TarInfo(name=Path(path).name)
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
