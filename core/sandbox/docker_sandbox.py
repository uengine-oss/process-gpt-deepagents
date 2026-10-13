"""core/sandbox/docker_sandbox.py — Docker 기반 격리 샌드박스 백엔드.

BaseSandbox를 상속해 execute()/upload_files()/download_files()를 구현한다.
tenant_id 당 하나의 컨테이너를 재사용하며, atexit 훅으로 프로세스 종료 시 정리한다.

컨테이너는 다음 볼륨을 마운트한다:
  - {workspace_host}/{tenant_id}/  →  /workspace  (read-write) — 볼륨 전체를 마운트하면
    에이전트가 셸로 다른 테넌트의 작업 파일을 그대로 읽는다(도구 레벨 경로 검사는 셸을
    막지 못한다). 그래서 마운트 단위로 가른다.
  - {skills_host}/{tenant_id}/ 및 알려진 글로벌 컬렉션만 각각 /skills/<name> 으로
    개별 bind-mount (read-only, optional) — skills_host 전체를 통째로 마운트하면
    다른 테넌트의 스킬 디렉토리까지 노출되므로 디렉토리 단위로 화이트리스트한다.

환경변수는 호스트에서 상속하지 않고 요청 tenant 의 `TENANT_ID` 만 넣는다.
"""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import io
import logging
import os
import re
import tarfile
import threading
import time
import zlib
from pathlib import Path, PurePosixPath

import docker
import docker.errors
from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox

from .tenant_workspace import tenant_workspace

logger = logging.getLogger(__name__)

SANDBOX_IMAGE = os.getenv("SANDBOX_IMAGE", "python:3.13-slim")
_CONTAINER_WORKSPACE = "/workspace"
_MAX_OUTPUT_BYTES = 100_000
_HOST_GATEWAY = "host.docker.internal"

# 컨테이너 구성(마운트·환경변수)을 바꾸면 올린다. 이름이 같은 컨테이너가 살아 있으면 재사용하므로,
# 라벨이 다르면 버리고 새로 띄워야 옛 구성이 배포 뒤에도 남지 않는다.
_CONTRACT_LABEL = "processgpt.sandbox.contract"
SANDBOX_CONTRACT = "tenant-workspace-v1"

# ---------------------------------------------------------------------
# 샌드박스 전용 브리지 네트워크 (MTU 고정)
#
# 기본 docker 브리지는 MTU 1500 으로 만들어진다. 그런데 이 서비스가 도는 GKE 처럼
# 노드/파드 네트워크 MTU 가 1460 이하인 환경에서는, 컨테이너가 MSS 1460 을 광고해 놓고
# 실제로는 그 크기의 프레임이 경로에서 통째로 드롭된다. ICMP fragmentation-needed 도
# 막혀 있어 PMTU discovery 가 동작하지 않으므로, 작은 패킷(DNS·TCP 핸드셰이크)은 되는데
# 큰 응답(TLS ServerHello+인증서, git pack)만 영원히 재전송되다가 ~150초 뒤
# "Recv failure: Connection reset by peer" 로 죽는다. 실제로 같은 컨테이너에서
# TCP_MAXSEG 를 1300 으로 낮추면 github.com TLS 핸드셰이크가 0.03초에 끝난다.
#
# 그래서 샌드박스 컨테이너는 기본 브리지 대신 MTU 를 낮춰 만든 전용 네트워크에 붙인다.
# SANDBOX_NETWORK_MTU=0 으로 두면 종전처럼 기본 브리지를 쓴다.
# ---------------------------------------------------------------------
SANDBOX_NETWORK_NAME = os.getenv("SANDBOX_NETWORK_NAME", "deepagent-sandbox-net")
try:
    SANDBOX_NETWORK_MTU = int(os.getenv("SANDBOX_NETWORK_MTU", "1400") or 0)
except ValueError:
    SANDBOX_NETWORK_MTU = 1400

# 샌드박스에서 사실상 항상 필요한 커맨드. 컨테이너를 새로 띄울 때 백그라운드로 미리 깔아
# 첫 git 호출이 apt-get 설치(수십 초)를 대신 물지 않게 한다.
_BOOTSTRAP_PACKAGES = ("git", "ca-certificates", "curl")
# 사전 설치가 아직 진행 중일 때 execute() 가 기다려 주는 최대 시간(초).
_BOOTSTRAP_WAIT_SECONDS = 120

# ---------------------------------------------------------------------
# 구간별 시간 상한
#
# 툴 호출 한 번이 언제 끝날지 알 수 없으면 대화가 통째로 멈춘다. 실제로 무한정
# 매달릴 수 있는 구간은 네 곳이다.
#   - 컨테이너 기동: dind 가 아직 준비되지 않으면 재시도 루프가 계속 돈다.
#   - 도구 설치:     apt 미러가 막힌 환경에서 apt-get 이 끝나지 않는다.
#   - 셸 실행:       사용자/에이전트가 준 명령 자체가 끝나지 않는다.
#   - 설정 조회:     docker 데몬이 응답하지 않으면 git config 한 줄도 돌아오지 않는다.
# 각 구간에 상한을 걸고, 넘으면 예외가 아니라 "사유가 적힌 결과"로 돌려준다 —
# 에이전트 툴 경로에서 예외는 원인 없는 실패로만 보이고 폴백을 고를 수 없기 때문이다.
# ---------------------------------------------------------------------

def _env_seconds(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


# 컨테이너 기동(dind 대기 재시도 포함) 상한.
SANDBOX_START_TIMEOUT = _env_seconds("SANDBOX_START_TIMEOUT_SECONDS", 90)
# 셸 명령 한 번(누락 커맨드 자동 설치 재시도까지 포함) 상한.
SANDBOX_EXEC_TIMEOUT = _env_seconds("SANDBOX_EXEC_TIMEOUT_SECONDS", 180)
# 누락 커맨드 자동 설치(apt-get update + install) 상한.
SANDBOX_INSTALL_TIMEOUT = _env_seconds("SANDBOX_INSTALL_TIMEOUT_SECONDS", 120)
# git safe.directory 적용 등 설정 조회/적용 상한.
SANDBOX_CONFIG_TIMEOUT = _env_seconds("SANDBOX_CONFIG_TIMEOUT_SECONDS", 15)

TIMEOUT_EXIT_CODE = 124
UNAVAILABLE_EXIT_CODE = 125


class SandboxUnavailable(RuntimeError):
    """상한 안에 쓸 수 있는 컨테이너를 얻지 못했다."""


def _exec_in_container(container, cmd: str, timeout: int) -> tuple[int, str, bool]:
    """컨테이너에서 `bash -c cmd` 를 실행하고 (exit_code, output, timed_out) 을 돌려준다.

    docker SDK 의 exec_run 에는 상한이 없어서 명령이 끝나지 않으면 호출 스레드가 영원히
    잡힌다. 실행을 데몬 스레드로 밀고 join(timeout) 으로 상한을 건다 — 시간이 지나면
    결과를 버리고 돌아온다. 컨테이너는 일회용이라 남은 프로세스는 정리 시 함께 사라진다.
    """
    holder: dict = {}

    def _run() -> None:
        try:
            exit_code, output = container.exec_run(["bash", "-c", cmd], demux=False)
            holder["exit_code"] = exit_code
            holder["output"] = (output or b"").decode("utf-8", errors="replace")
        except Exception as exc:  # 데몬 오류도 상한 안에서 "사유"로 돌려준다
            holder["exit_code"] = 1
            holder["output"] = f"ERROR: {exc}"

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return TIMEOUT_EXIT_CODE, f"ERROR: command timed out after {timeout}s", True
    return holder.get("exit_code") or 0, holder.get("output", ""), False

# ---------------------------------------------------------------------
# 실행(run) 격리 스코프 — 같은 tenant 는 컨테이너/마운트를 공유하므로(위 docstring 참고),
# ls/read_file/glob/grep 이 인자로 받은 절대경로를 그대로 열어주면 "다른 실행(예: 일주일 전
# 같은 액티비티 실행)이 남긴 산출물"을 이번 실행 결과인 것처럼 읽어오는 사고가 난다.
# executor 가 실행 시작 시 set_workspace_scope() 로 이번 실행의 허용 경로를 등록하면,
# 그 경로 밖의 "다른 실행 전용" 산출물 트리(.bpmn/<다른 방>, <다른 instance_id>/inputs·outputs)
# 접근을 거부한다. contextvar 를 쓰는 이유는 컨테이너(=DockerSandboxBackend 인스턴스)가
# tenant 당 하나로 재사용되어 여러 실행이 동시에 같은 인스턴스를 공유하기 때문 —
# 인스턴스 속성에 저장하면 동시 실행끼리 스코프가 뒤섞인다.
# ---------------------------------------------------------------------
_workspace_scope: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "sandbox_workspace_scope", default=()
)

# _instance_ws_dir()(executor.py) 가 만드는 inputs/outputs 디렉토리명은 root_proc_inst_id 등
# UUID 를 포함한 instance_id 를 sanitize 한 값이다. checkout_to_workspace 가 만드는 저장소/스킬
# 체크아웃 디렉토리명(예: /workspace/my-repo)과 구별하기 위해 "이름에 UUID 가 포함되어 있는가"를
# 판별 기준으로 쓴다 — 일반 저장소 이름이 우연히 UUID 를 포함할 가능성은 사실상 없다.
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)

_SCOPE_DENY_MSG = (
    "다른 실행(run)의 산출물 경로입니다 — 이번 실행에서 만든 파일이 아닙니다. "
    "과거 실행 결과를 재사용하지 말고 이번 실행에서 직접 조회/생성하세요."
)


def set_workspace_scope(*container_prefixes: str) -> None:
    """이번 실행이 ls/read_file/glob/grep 으로 접근 가능한 /workspace 하위 경로를 등록한다.

    인자를 비우면(기본값) 제한하지 않는다 — 이 함수를 호출하지 않는 기존 호출부(예:
    executor.py 의 보조 헬퍼가 단발성으로 만드는 백엔드)는 종전처럼 무제한으로 동작한다.
    """
    _workspace_scope.set(tuple(p.rstrip("/") for p in container_prefixes if p))


# core/skills/tools.py 의 BPMN_WORK_SUBDIR / CHECKOUT_SUBDIR 와 같은 값.
# (여기서 tools 를 import 하면 샌드박스가 스킬 계층에 의존하게 되므로 리터럴로 둔다.)
_ROOM_SCOPED_SUBDIRS = (".bpmn", ".checkouts")


def _is_run_scoped_path(path: str) -> bool:
    """다른 실행 전용 트리(.bpmn/<방>, .checkouts/<방>, <instance_id>/inputs·outputs)인가."""
    parts = path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] == "workspace" and parts[1] in _ROOM_SCOPED_SUBDIRS:
        return True
    if (
        len(parts) >= 3
        and parts[0] == "workspace"
        and parts[2] in ("inputs", "outputs")
        and _UUID_RE.search(parts[1])
    ):
        return True
    return False

# 컨테이너로 전달할 호스트 env 화이트리스트.
#
# ⚠️ Supabase 자격증명(SUPABASE_URL/SERVICE_ROLE_KEY 등)은 **의도적으로 전달하지 않는다.**
# 샌드박스에는 에이전트가 임의 셸을 실행할 수 있으므로, 여기에 service_role 키를 넘기면
# "스킬·에이전트는 DB 에 직접 쓰지 않는다" 는 원칙이 프롬프트 금지문구로만 지켜지게 된다.
# 프로세스/스킬/에이전트의 임시저장·최종저장은 모두 프론트가 사용자 자격증명으로 수행한다.
# 실행엔진 검증도 프론트의 /validate-and-improve 가 담당하므로 COMPLETION_ENGINE_URL 도 넘기지 않는다.
#
# 모델 API 키도 넘기지 않는다. 사용자가 채팅으로 `env` 를 시키면 그대로 출력돼 실제로 노출됐다.
# 샌드박스 스크립트가 모델을 불러야 한다면 원본 키가 아니라 범위가 제한된 토큰을 따로 둬야 한다
# (bpmn-process-generation-skill 의 자동교정은 자격증명이 없으면 검증만 하고 넘어간다).
def _build_container_env(tenant_id: str) -> dict[str, str]:
    """요청 tenant 만 주입한다(스킬 scripts 가 --tenant 없이 TENANT_ID 로 읽는다)."""
    return {"TENANT_ID": tenant_id or ""}

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
        # 볼륨 루트가 아니라 이 테넌트 디렉터리만 /workspace 로 마운트한다.
        self._workspace_host = tenant_workspace(workspace_host, tenant_id)
        self._skills_host = skills_host
        self._container: docker.models.containers.Container | None = None
        self._lock = threading.Lock()
        self._client: docker.DockerClient | None = None

    # ------------------------------------------------------------------
    # 컨테이너 생명주기
    # ------------------------------------------------------------------

    def _ensure_container(
        self, *, deadline: float | None = None
    ) -> docker.models.containers.Container:
        """상한 안에 쓸 수 있는 컨테이너를 돌려준다. 못 얻으면 SandboxUnavailable.

        락 대기도 상한에 포함한다 — 같은 테넌트의 다른 실행이 컨테이너를 띄우는 중이면
        여기서 무한정 기다리게 되고, 그러면 호출자가 건 상한이 아무 의미가 없다.
        """
        if deadline is None:
            deadline = time.monotonic() + SANDBOX_START_TIMEOUT
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._lock.acquire(timeout=remaining):
            raise SandboxUnavailable(
                f"샌드박스 컨테이너를 {SANDBOX_START_TIMEOUT}초 안에 준비하지 못했습니다"
                "(다른 작업이 컨테이너를 기동 중)."
            )
        try:
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

            self._container = self._start_container_with_retry(deadline=deadline)
            return self._container
        finally:
            self._lock.release()

    def _start_container_with_retry(
        self, *, deadline: float, initial_delay: float = 2.0
    ) -> docker.models.containers.Container:
        """dind가 아직 준비 중일 때 exponential backoff로 재시도한다.

        재시도 횟수가 아니라 **남은 시간**으로 끊는다. 횟수로만 끊으면 backoff 합이
        2분을 넘겨(2+3+4.5+…) 툴 호출 하나가 그만큼 매달린다.
        """
        delay = initial_delay
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._start_container()
            except docker.errors.DockerException as exc:
                remaining = deadline - time.monotonic()
                if remaining <= delay:
                    raise SandboxUnavailable(
                        f"샌드박스 컨테이너를 {SANDBOX_START_TIMEOUT}초 안에 띄우지 못했습니다: {exc}"
                    ) from exc
                logger.warning(
                    "Docker not ready (attempt %d, %.0fs left): %s — retrying in %.1fs",
                    attempt, remaining, exc, delay,
                )
                time.sleep(delay)
                delay = min(delay * 1.5, 30.0)
                self._client = _get_docker_client()

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

    def _ensure_network(self) -> str | None:
        """MTU 를 낮춰 잡은 샌드박스 전용 브리지 네트워크 이름을 반환한다.

        생성/조회에 실패하면 None 을 돌려주고 호출자는 기본 브리지로 진행한다 —
        네트워크를 못 만든다고 샌드박스 자체를 못 쓰게 만들 이유는 없다.
        """
        if not SANDBOX_NETWORK_MTU:
            return None
        try:
            self._client.networks.get(SANDBOX_NETWORK_NAME)
            return SANDBOX_NETWORK_NAME
        except docker.errors.NotFound:
            pass
        except docker.errors.APIError as exc:
            logger.warning("샌드박스 네트워크 조회 실패, 기본 브리지 사용: %s", exc)
            return None
        try:
            self._client.networks.create(
                SANDBOX_NETWORK_NAME,
                driver="bridge",
                options={"com.docker.network.driver.mtu": str(SANDBOX_NETWORK_MTU)},
            )
            logger.info(
                "샌드박스 네트워크 생성: %s (mtu=%d)", SANDBOX_NETWORK_NAME, SANDBOX_NETWORK_MTU
            )
        except docker.errors.APIError as exc:
            # 동시 기동 경합으로 이미 만들어졌을 수 있다.
            try:
                self._client.networks.get(SANDBOX_NETWORK_NAME)
            except Exception:
                logger.warning("샌드박스 네트워크 생성 실패, 기본 브리지 사용: %s", exc)
                return None
        return SANDBOX_NETWORK_NAME

    def _start_container(self) -> docker.models.containers.Container:
        name = f"deepagent-sandbox-{self._tenant_id or 'shared'}"
        network = self._ensure_network()

        # 이전 실행에서 남은 중단된 컨테이너 정리
        try:
            old = self._client.containers.get(name)
            if old.status == "running":
                if not self._contract_matches(old):
                    # 옛 구성(볼륨 전체 마운트·모델 키 env)으로 떠 있는 컨테이너다.
                    logger.info("sandbox 재생성: %s 의 구성이 %s 가 아님", name, SANDBOX_CONTRACT)
                elif self._network_matches(old, network):
                    self._container = old
                    # 이전 실행에서 살아남은 컨테이너에도 safe.directory 를 보장한다.
                    self._configure_git(old)
                    self._bootstrap_tooling(old)
                    return old
                else:
                    # 예전 배포가 기본 브리지(MTU 1500)로 띄워 둔 컨테이너다. 그대로 재사용하면
                    # git clone/push 가 계속 매달리므로, 일회용 컨테이너답게 버리고 다시 띄운다.
                    # /workspace 는 볼륨이라 재생성해도 산출물은 남는다.
                    logger.info("sandbox 재생성: %s 가 %s 네트워크에 없음", name, network)
            old.remove(force=True)
        except docker.errors.NotFound:
            pass

        self._workspace_host.mkdir(parents=True, exist_ok=True)
        volumes: dict = {str(self._workspace_host): {"bind": _CONTAINER_WORKSPACE, "mode": "rw"}}
        volumes.update(self._skill_volumes())

        run_kwargs: dict = dict(
            command=["sleep", "infinity"],
            name=name,
            detach=True,
            auto_remove=True,
            volumes=volumes,
            working_dir=_CONTAINER_WORKSPACE,
            environment=_build_container_env(self._tenant_id),
            labels={_CONTRACT_LABEL: SANDBOX_CONTRACT},
            extra_hosts={_HOST_GATEWAY: "host-gateway"},
            mem_limit="512m",
            cpu_period=100_000,
            cpu_quota=50_000,  # 50% of one CPU core
        )
        if network:
            run_kwargs["network"] = network

        container = self._client.containers.run(SANDBOX_IMAGE, **run_kwargs)
        logger.info(
            "sandbox started: %s (tenant=%s, network=%s)", name, self._tenant_id, network or "bridge"
        )
        self._configure_git(container)
        self._bootstrap_tooling(container)
        return container

    @staticmethod
    def _contract_matches(container) -> bool:
        """컨테이너가 현재 구성 버전으로 떠 있는지. 확인하지 못하면 재사용하지 않는다."""
        try:
            container.reload()
            labels = (container.attrs.get("Config") or {}).get("Labels") or {}
        except Exception:
            return False
        return labels.get(_CONTRACT_LABEL) == SANDBOX_CONTRACT

    @staticmethod
    def _network_matches(container, network: str | None) -> bool:
        """컨테이너가 원하는 네트워크에 붙어 있는지 확인한다."""
        if not network:
            return True
        try:
            container.reload()
            attached = (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
            return network in attached
        except Exception:  # pragma: no cover - best effort
            return True

    def _bootstrap_tooling(self, container) -> None:
        """git/curl 을 백그라운드로 미리 설치한다 (컨테이너당 1회).

        샌드박스 이미지(python:3.13-slim)에는 git 이 없어서, 아무 조치가 없으면 사용자의
        첫 git 명령이 execute() 의 자동설치 경로로 apt-get update+install(30초 이상)을
        그대로 뒤집어쓴다. 컨테이너를 띄우는 시점에 미리 깔아 두면 그 지연이 대화 중간이
        아니라 기동 시점으로 옮겨간다. 실패해도 execute() 의 기존 자동설치가 그대로 백업이다.
        """
        if getattr(container, "_deepagent_bootstrapped", False):
            return
        container._deepagent_bootstrapped = True
        pkgs = " ".join(_BOOTSTRAP_PACKAGES)
        cmd = (
            "command -v git >/dev/null 2>&1 && exit 0; "
            "DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1; "
            f"apt-get install -y -qq --no-install-recommends {pkgs} >/dev/null 2>&1"
        )

        def _run() -> None:
            try:
                _, _, timed_out = _exec_in_container(container, cmd, SANDBOX_INSTALL_TIMEOUT)
                if timed_out:
                    logger.warning(
                        "sandbox 기본 툴 사전 설치가 %d초 상한을 넘겨 중단했습니다",
                        SANDBOX_INSTALL_TIMEOUT,
                    )
                    return
                self._configure_git(container)
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("sandbox 기본 툴 사전 설치 실패(무시): %s", exc)

        thread = threading.Thread(target=_run, daemon=True)
        container._deepagent_bootstrap_thread = thread
        thread.start()

    @staticmethod
    def _configure_git(container) -> None:
        """/workspace 를 git 이 신뢰하도록 safe.directory 를 설정한다.

        /workspace 는 호스트 볼륨 마운트라 디렉터리 소유자 uid 가 컨테이너 사용자와 다르다.
        그러면 git 이 `fatal: detected dubious ownership in repository at '/workspace/<skill>'`
        로 모든 명령을 거부하고, publish_skill 은 "레포는 만들어졌는데 push 는 실패" 한
        빈 레포 상태를 남긴다. 컨테이너는 이미 격리된 일회용 환경이므로 여기서 신뢰를 넓혀도
        호스트 git 설정에는 영향이 없다. git 이 아직 없으면 조용히 실패하고, 이후 자동 설치
        직후 다시 호출된다.
        """
        try:
            _, _, timed_out = _exec_in_container(
                container,
                "git config --system --add safe.directory '*' 2>/dev/null || true",
                SANDBOX_CONFIG_TIMEOUT,
            )
            if timed_out:
                logger.warning(
                    "sandbox git 설정 적용이 %d초 상한을 넘겨 건너뜁니다", SANDBOX_CONFIG_TIMEOUT
                )
        except Exception as e:  # pragma: no cover - best effort
            logger.debug("sandbox git safe.directory 설정 실패(무시): %s", e)

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

    # ------------------------------------------------------------------
    # 실행 격리 스코프 강제 — set_workspace_scope() 로 등록된 이번 실행의 허용 경로
    # 밖에 있는 "다른 실행 전용" 산출물(.bpmn/<다른 방>, <다른 instance_id>/inputs·outputs)에
    # 대한 읽기 계열 호출(ls/read/glob/grep)을 차단한다. write/edit 은 제한하지 않는다 —
    # 에이전트가 스스로 고른 새 경로에 쓰는 것이라 다른 실행 결과를 "재사용"하는 경로가 아니고,
    # checkout_to_workspace 가 이번 방의 체크아웃 트리를 새로 만들며 쓰기 때문에,
    # write 까지 막으면 그 정상 흐름을 깬다.
    # ------------------------------------------------------------------

    def _in_workspace_scope(self, path: str) -> bool:
        if not path or not path.startswith(_CONTAINER_WORKSPACE):
            return True  # /skills 등 workspace 밖은 별도 마운트로 이미 격리됨
        if not _is_run_scoped_path(path):
            return True  # uploads 등 방과 무관한 공용 영역은 제한하지 않음
        scope = _workspace_scope.get()
        if not scope:
            return True  # 스코프 미설정 호출부(레거시)는 종전처럼 무제한
        norm = path.rstrip("/")
        return any(norm == p or norm.startswith(p + "/") for p in scope)

    def ls(self, path: str) -> LsResult:
        if not self._in_workspace_scope(path):
            return LsResult(entries=None, error=f"Path '{path}': {_SCOPE_DENY_MSG}")
        return super().ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if not self._in_workspace_scope(file_path):
            return ReadResult(error=f"File '{file_path}': {_SCOPE_DENY_MSG}")
        return super().read(file_path, offset, limit)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        scope = _workspace_scope.get()
        effective_path = path or (scope[0] if scope else None)
        if effective_path is not None and not self._in_workspace_scope(effective_path):
            return GlobResult(matches=None, error=f"Path '{effective_path}': {_SCOPE_DENY_MSG}")
        return super().glob(pattern, effective_path)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        scope = _workspace_scope.get()
        effective_path = path or (scope[0] if scope else None)
        if effective_path is not None and not self._in_workspace_scope(effective_path):
            return GrepResult(error=f"Path '{effective_path}': {_SCOPE_DENY_MSG}")
        return super().grep(pattern, effective_path, glob)

    # ------------------------------------------------------------------
    # 같은 파일에 대한 쓰기 직렬화
    #
    # BaseSandbox.edit 은 컨테이너 안에서 "파일을 읽고 → 치환 → 'wb' 로 열어 다시 쓰기" 를
    # 한다. 모델이 한 턴에 같은 파일로 edit_file 을 여러 개 내면 미들웨어가 각각을
    # asyncio.to_thread 로 동시에 돌리는데, 그러면 한쪽이 'wb' 로 파일을 비운 사이 다른
    # 쪽이 반쯤 쓰인 내용을 읽거나 두 쓰기가 섞여 멀티바이트(한글) 문자가 잘린다. 그 파일은
    # 이후 "is not a text file" 로 편집이 막히고, read_file 이 base64(`file` 블록)로 돌려줘
    # 모델 호출이 400 으로 실패한다. 경로 단위로 잠가 한 번에 하나씩만 쓰게 한다.
    #
    # 잠금은 경로 해시로 고른 고정 개수(스트라이프)만 둔다 — 경로마다 만들면 오래 도는
    # 서비스에서 끝없이 늘어난다. 다른 파일이 같은 칸에 걸리면 잠시 기다릴 뿐 결과는 같다.
    # ------------------------------------------------------------------
    _FILE_LOCK_STRIPES = 64
    _file_locks = tuple(threading.Lock() for _ in range(_FILE_LOCK_STRIPES))

    def _file_lock(self, file_path: str) -> threading.Lock:
        key = f"{getattr(self, '_tenant_id', '') or ''}:{PurePosixPath(file_path).as_posix()}"
        index = zlib.crc32(key.encode("utf-8")) % self._FILE_LOCK_STRIPES
        return self._file_locks[index]

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,  # noqa: FBT001, FBT002
    ) -> EditResult:
        with self._file_lock(file_path):
            return super().edit(file_path, old_string, new_string, replace_all)

    def write(self, file_path: str, content: str) -> WriteResult:
        with self._file_lock(file_path):
            return super().write(file_path, content)

    # deepagents 0.7 부터 BaseSandbox.aedit/awrite 는 동기 edit/write 를 거치지 않고
    # aexecute/aupload_files 로 바로 간다. 그대로 두면 미들웨어(비동기 경로)의 편집이 위
    # 잠금을 우회해 같은 파일 동시 편집으로 파일이 깨지던 문제가 되살아나므로, 잠금이 걸린
    # 동기 경로로 되돌린다.
    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,  # noqa: FBT001, FBT002
    ) -> EditResult:
        return await asyncio.to_thread(self.edit, file_path, old_string, new_string, replace_all)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return await asyncio.to_thread(self.write, file_path, content)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """셸 명령을 실행한다. 총 소요는 기동 상한 + 실행 상한을 넘지 않는다.

        상한을 넘기면 예외 대신 exit_code 124(시간 초과)/125(샌드박스 사용 불가)와
        사유 문구를 돌려준다 — 호출자(스킬 도구)가 그걸 보고 로컬 경로로 폴백하고
        사용자에게 알릴 수 있어야 하기 때문이다.
        """
        try:
            container = self._ensure_container()
        except SandboxUnavailable as exc:
            return ExecuteResponse(output=f"ERROR: {exc}", exit_code=UNAVAILABLE_EXIT_CODE, truncated=False)
        except docker.errors.DockerException as exc:
            return ExecuteResponse(
                output=f"ERROR: 샌드박스를 사용할 수 없습니다: {exc}",
                exit_code=UNAVAILABLE_EXIT_CODE,
                truncated=False,
            )

        _timeout = timeout or SANDBOX_EXEC_TIMEOUT
        # 자동 설치 재시도까지 포함한 **한 호출 전체**의 마감 시각. 재시도마다 상한을
        # 새로 주면 (첫 실행 + 사전설치 대기 + apt + 재실행) 이 상한의 몇 배가 된다.
        deadline = time.monotonic() + _timeout

        def _remaining() -> int:
            return int(deadline - time.monotonic())

        def _timeout_response() -> tuple[int, str]:
            return TIMEOUT_EXIT_CODE, (
                f"ERROR: command timed out after {_timeout}s — 상한을 넘겨 중단했습니다. "
                "명령을 더 작게 나누거나 오래 걸리는 단계를 건너뛰고 다시 시도하세요."
            )

        def _exec(cmd: str) -> tuple[int, str]:
            budget = _remaining()
            if budget <= 0:
                return _timeout_response()
            # Don't pass workdir= to exec_run: on macOS + Colima/Docker Desktop,
            # runc resolves the bind-mount target back to the host path and
            # raises "current working directory is outside of container mount
            # namespace root". cd inside bash bypasses that OCI-level check.
            #
            # 서브셸 여는/닫는 괄호는 반드시 **각자 자기 줄**에 둔다. cmd 와 같은 줄에
            # ` )` 를 붙이면, cmd 가 heredoc(<<'JSON' ... JSON)으로 끝날 때 닫는 구분자
            # 줄이 "JSON )" 로 오염돼 heredoc 이 깨진다(bash: here-document ... wanted `JSON').
            wrapped = f"cd {_CONTAINER_WORKSPACE} && (\n{cmd}\n)"
            exit_code, output, timed_out = _exec_in_container(container, wrapped, budget)
            if timed_out:
                return _timeout_response()
            return exit_code, output

        exit_code, out = _exec(command)
        notes: list[str] = []

        # 커맨드를 찾을 수 없는 경우 apt-get으로 자동 설치 후 1회 재시도.
        # 같은 커맨드는 컨테이너당 한 번만 시도한다 — apt 를 쓸 수 없는 환경(사설 네트워크,
        # 미러 차단 등)에서는 설치가 매번 실패하는데, 그때마다 apt-get update 를 다시 도는
        # 것은 툴 호출마다 수십 초를 그냥 버리는 짓이다.
        match = _CMD_NOT_FOUND_RE.search(out)
        if match:
            missing = match.group(1)
            attempted = getattr(container, "_deepagent_apt_attempted", None)
            if attempted is None:
                attempted = set()
                container._deepagent_apt_attempted = attempted
            # 사전 설치가 아직 돌고 있으면 그게 끝나기를 기다린 뒤 한 번 더 본다.
            # 기다리지 않고 여기서 apt 를 또 부르면 dpkg 락을 두고 서로 막는다.
            bootstrap = getattr(container, "_deepagent_bootstrap_thread", None)
            if bootstrap is not None and bootstrap.is_alive():
                wait = min(_BOOTSTRAP_WAIT_SECONDS, max(_remaining(), 0))
                bootstrap.join(wait)
                if bootstrap.is_alive():
                    notes.append(
                        f"NOTE: 기본 도구 사전 설치가 {wait}초 안에 끝나지 않아 기다리지 않고 진행했습니다."
                    )
                else:
                    exit_code, out = _exec(command)
                    match = _CMD_NOT_FOUND_RE.search(out)
                    missing = match.group(1) if match else ""

            install_budget = min(SANDBOX_INSTALL_TIMEOUT, max(_remaining(), 0))
            if missing and missing not in attempted and install_budget > 0:
                attempted.add(missing)
                pkg = _PACKAGE_OVERRIDES.get(missing, missing)
                logger.info("auto-installing missing command %r (package=%r)", missing, pkg)
                install_cmd = (
                    "DEBIAN_FRONTEND=noninteractive apt-get update -qq 2>&1 | tail -1 "
                    f"&& apt-get install -y -qq --no-install-recommends {pkg} 2>&1 | tail -3"
                )
                _, _, install_timed_out = _exec_in_container(container, install_cmd, install_budget)
                if install_timed_out:
                    # 폴백: 설치를 포기하고 원래 결과(=커맨드 없음)를 그대로 돌려준다.
                    # 사용자에게는 그 사실을 알려 다른 방법을 고를 수 있게 한다.
                    logger.warning(
                        "sandbox 자동 설치가 %d초 상한을 넘겨 중단: %s", install_budget, missing
                    )
                    notes.append(
                        f"NOTE: '{missing}' 자동 설치가 {install_budget}초 상한을 넘겨 중단했습니다. "
                        "설치 없이 진행하니, 이 명령이 필요 없는 방법으로 다시 시도하세요."
                    )
                else:
                    if missing == "git":
                        self._configure_git(container)
                    exit_code, out = _exec(command)
            elif missing and missing in attempted:
                logger.info("auto-install skipped for %r (already attempted in this sandbox)", missing)
            elif missing:
                notes.append(
                    f"NOTE: 남은 시간이 없어 '{missing}' 자동 설치를 건너뛰었습니다."
                )

        if notes:
            out = f"{out}\n" + "\n".join(notes) if out else "\n".join(notes)

        truncated = len(out) > _MAX_OUTPUT_BYTES
        if truncated:
            out = out[:_MAX_OUTPUT_BYTES] + "\n...[truncated]"
        return ExecuteResponse(
            output=out,
            exit_code=exit_code,
            truncated=truncated,
        )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        try:
            container = self._ensure_container()
        except (SandboxUnavailable, docker.errors.DockerException) as exc:
            return [FileUploadResponse(path=path, error=str(exc)) for path, _ in files]
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
        try:
            container = self._ensure_container()
        except (SandboxUnavailable, docker.errors.DockerException) as exc:
            return [FileDownloadResponse(path=path, error=str(exc)) for path in paths]
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
    """tenant_id에 대응하는 샌드박스 백엔드를 반환한다. 없으면 새로 생성한다.

    tenant_id 가 비었거나 디렉터리 이름으로 쓸 수 없으면 ValueError — 공용 작업공간으로
    떨어지면 테넌트끼리 파일이 섞인다.
    """
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
