"""core/sandbox/docker_sandbox.py — Docker 기반 격리 샌드박스 백엔드.

BaseSandbox를 상속해 execute()/upload_files()/download_files()를 구현한다.
tenant_id 당 하나의 컨테이너를 재사용하며, atexit 훅으로 프로세스 종료 시 정리한다.

컨테이너는 다음 볼륨을 마운트한다:
  - {workspace_host}/  →  /workspace  (read-write)
  - {skills_host}/{tenant_id}/ 및 알려진 글로벌 컬렉션만 각각 /skills/<name> 으로
    개별 bind-mount (read-only, optional) — skills_host 전체를 통째로 마운트하면
    다른 테넌트의 스킬 디렉토리까지 노출되므로 디렉토리 단위로 화이트리스트한다.

환경변수는 호스트 전체를 상속하지 않고 **화이트리스트(_FORWARD_ENV_KEYS)만** 컨테이너로
전달하며, 여기에 Supabase 자격증명은 포함하지 않는다(샌드박스에서 DB 직접 쓰기 차단).
또한 요청이 들어온 tenant 로 `TENANT_ID` 를 자동 세팅한다.
"""

from __future__ import annotations

import atexit
import io
import logging
import os
import re
import tarfile
import threading
import time
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
_HOST_GATEWAY = "host.docker.internal"

# 컨테이너로 전달할 호스트 env 화이트리스트.
#
# ⚠️ Supabase 자격증명(SUPABASE_URL/SERVICE_ROLE_KEY 등)은 **의도적으로 전달하지 않는다.**
# 샌드박스에는 에이전트가 임의 셸을 실행할 수 있으므로, 여기에 service_role 키를 넘기면
# "스킬·에이전트는 DB 에 직접 쓰지 않는다" 는 원칙이 프롬프트 금지문구로만 지켜지게 된다.
# 프로세스/스킬/에이전트의 임시저장·최종저장은 모두 프론트가 사용자 자격증명으로 수행한다.
# 실행엔진 검증도 프론트의 /validate-and-improve 가 담당하므로 COMPLETION_ENGINE_URL 도 넘기지 않는다.
_FORWARD_ENV_KEYS = (
    # LLM (core/model.py 와 동일 변수명) — 샌드박스에서 도는 스킬들의 모델 호출용
    "LLM_MODEL", "LLM_PROXY_URL", "LLM_PROXY_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
)

def _build_container_env(tenant_id: str) -> dict[str, str]:
    """화이트리스트 host env + 요청 tenant 자동주입(TENANT_ID)."""
    env: dict[str, str] = {}
    for k in _FORWARD_ENV_KEYS:
        v = os.environ.get(k)
        if v:
            env[k] = v
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

            self._container = self._start_container_with_retry()
            return self._container

    def _start_container_with_retry(
        self, *, max_attempts: int = 10, initial_delay: float = 2.0
    ) -> docker.models.containers.Container:
        """dind가 아직 준비 중일 때 exponential backoff로 재시도한다."""
        delay = initial_delay
        for attempt in range(max_attempts):
            try:
                return self._start_container()
            except docker.errors.DockerException as exc:
                if attempt >= max_attempts - 1:
                    raise
                logger.warning(
                    "Docker not ready (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1, max_attempts, exc, delay,
                )
                time.sleep(delay)
                delay = min(delay * 1.5, 30.0)
                self._client = _get_docker_client()
        raise RuntimeError("unreachable")

    def _skill_volumes(self) -> dict:
        """이 테넌트 디렉토리와 알려진 글로벌 컬렉션만 개별 bind-mount한다.

        skills_host 전체를 한 번에 마운트하면 같은 루트에 있는 다른 테넌트의
        스킬 디렉토리까지 컨테이너의 /skills 트리에 그대로 노출되어, ls/read_file
        같은 파일시스템 도구로 직접 열람할 수 있게 된다(core/skills/skills.py 의
        _get_skills 화이트리스트는 시스템 프롬프트 노출만 막을 뿐 마운트 자체는
        막지 못함). 디렉토리 단위로 개별 마운트해 컨테이너 안에 다른 테넌트
        항목이 애초에 존재하지 않도록 한다.
        """
        if not self._skills_host or not self._skills_host.exists():
            return {}
        from core.skills.skills import _global_collection_names

        allowed = _global_collection_names()
        if self._tenant_id:
            allowed = allowed | {self._tenant_id}

        volumes: dict = {}
        try:
            for entry in sorted(self._skills_host.iterdir()):
                if entry.is_dir() and entry.name in allowed:
                    volumes[str(entry)] = {"bind": f"/skills/{entry.name}", "mode": "ro"}
        except OSError:
            pass
        return volumes

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
        volumes.update(self._skill_volumes())

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
