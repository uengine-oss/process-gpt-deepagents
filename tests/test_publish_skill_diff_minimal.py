"""기존 레포 수정 시 PR diff 가 최소로 남는지 실제 git 으로 검증한다.

관측된 문제: 스킬 한 줄을 고쳐 PR 을 올렸는데 diff 에 레포에만 있던 파일(evals/*.docx)의
삭제와 SKILL.md 전체 교체가 섞여 나왔다. 원인은 `git init` 후 `reset --soft` 로 HEAD 만
옮겨 **인덱스가 빈 상태**로 남는 것 — 그러면 원격 HEAD 의 모든 파일이 삭제로 보인다.

여기서는 _push_skill_sandbox 의 git 시퀀스를 실제 git 저장소에 재현해,
`--soft`(구 동작)와 `--mixed`+`--ignore-removal`(현 동작)의 결과 차이를 고정한다.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git 이 필요합니다")


def _git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e",
        "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd),
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    ).stdout


@pytest.fixture
def remote_and_worktree(tmp_path):
    """원격 레포(SKILL.md + evals/keep.docx)와, 로컬 스킬 사본만 든 워킹트리를 만든다."""
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(remote, "init", "-b", "main")
    (remote / "SKILL.md").write_text("line1\nline2\n", encoding="utf-8")
    (remote / "evals").mkdir()
    (remote / "evals" / "keep.docx").write_text("eval fixture", encoding="utf-8")
    _git(remote, "add", ".")
    _git(remote, "commit", "-m", "init")

    # 워킹트리 = 로컬 스킬 사본. evals/ 가 없고 SKILL.md 는 한 줄만 다르다.
    work = tmp_path / "work"
    work.mkdir()
    (work / "SKILL.md").write_text("line1\nline2\nline3-added\n", encoding="utf-8")
    return remote, work


def _run_publish_sequence(remote: Path, work: Path, *, reset_mode: str, add_args: list[str]):
    """_push_skill_sandbox 의 git 시퀀스를 재현하고 HEAD 대비 변경 목록을 돌려준다."""
    _git(work, "init", "-b", "main")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "fetch", "origin", "main", "--depth=1")
    _git(work, "reset", reset_mode, "FETCH_HEAD")
    _git(work, "add", *add_args)
    _git(work, "commit", "-m", "feat: update")
    out = _git(work, "diff", "--name-status", "FETCH_HEAD", "HEAD")
    return [l.split("\t") for l in out.strip().splitlines() if l.strip()]


def test_soft_reset_produces_spurious_deletions(remote_and_worktree):
    """구 동작 재현 — 인덱스가 비어 evals/ 삭제가 diff 에 섞인다(회귀 감시용)."""
    remote, work = remote_and_worktree
    changes = _run_publish_sequence(remote, work, reset_mode="--soft", add_args=["."])
    deleted = [c[1] for c in changes if c[0].startswith("D")]
    assert "evals/keep.docx" in deleted, changes


def test_mixed_reset_with_ignore_removal_keeps_diff_minimal(remote_and_worktree):
    """현 동작 — 요청한 파일만 수정되고 레포 전용 파일은 건드리지 않는다."""
    remote, work = remote_and_worktree
    changes = _run_publish_sequence(
        remote, work, reset_mode="--mixed", add_args=["--ignore-removal", "."]
    )

    paths = {c[1] for c in changes}
    statuses = {c[1]: c[0] for c in changes}

    assert paths == {"SKILL.md"}, f"SKILL.md 만 바뀌어야 한다: {changes}"
    assert statuses["SKILL.md"].startswith("M")
    assert not [c for c in changes if c[0].startswith("D")], f"삭제가 없어야 한다: {changes}"
