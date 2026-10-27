"""core/storage/worktree.py — 인계 산출물 워크트리(에이전트 쪽).

인계 스레드마다 저장소 하나, 방마다 브랜치 하나. 커밋은 스냅샷 전체를 tree 로 담는다
(방 작업 폴더 기준 상대 경로 → {sha256, size, content_type}). 내용은 `files` 버킷
`artifacts/{tenant}/objects/{sha256}` 에 한 번만 둔다. 화면의 src/utils/artifactWorktree.js 와
같은 규칙이다.

에이전트는 실행을 시작할 때 방 브랜치 head 를 작업 폴더로 펼치고(materialize), 턴이 끝나면
작업 폴더를 훑어 바뀐 경로만 head 위에 얹어 커밋한다(commit_turn). head 가 그새 바뀌었으면
(같은 방의 사람 커밋) commit_branch 가 40001 을 내고, 최신 head 위에서 다시 얹는다.
브랜치가 없는 방(예전 방·인계와 무관한 방)은 None 을 돌려주고 호출 쪽이 기존 방 폴더 동기화를 쓴다.

오케스트레이터 전용이다(에이전트 도구가 아니다). service-role 키를 쓰고, tenant_id 는
executor 가 넘긴 현재 요청 값으로 고정한다.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .db import get_supabase
from .storage import _BUCKET, _PREFIX, decode_key_path

logger = logging.getLogger(__name__)

RETRY_LIMIT = 5


@dataclass
class BranchState:
    """실행 시작 때 펼친 브랜치. 턴 끝 커밋이 이 tree 와 비교해 바뀐 경로를 찾는다."""

    room_id: str
    head_commit_id: str
    tree: dict = field(default_factory=dict)


def object_key(tenant_id: str, sha256: str) -> str:
    return f"artifacts/{tenant_id}/objects/{sha256}"


def _content_type(path: str) -> str:
    ct, _ = mimetypes.guess_type(path)
    return ct or "application/octet-stream"


def _is_duplicate(exc: Exception) -> bool:
    text = str(exc)
    return "409" in text or "Duplicate" in text or "already exists" in text


def put_object(tenant_id: str, data: bytes, path: str, client=None) -> dict:
    """내용을 객체로 올린다. 같은 내용이 이미 있으면 올리지 않는다."""
    sha = hashlib.sha256(data).hexdigest()
    ct = _content_type(path)
    client = client or get_supabase()
    try:
        client.storage.from_(_BUCKET).upload(
            path=object_key(tenant_id, sha), file=data, file_options={"content-type": ct, "upsert": "false"}
        )
    except Exception as exc:  # noqa: BLE001 — 중복은 성공이다
        if not _is_duplicate(exc):
            raise
    return {"sha256": sha, "size": len(data), "content_type": ct}


def read_object(tenant_id: str, sha256: str, client=None) -> bytes:
    client = client or get_supabase()
    return client.storage.from_(_BUCKET).download(object_key(tenant_id, sha256))


def _one(rows) -> dict | None:
    data = getattr(rows, "data", rows)
    return data[0] if isinstance(data, list) and data else None


def get_branch_head(room_id: str, client=None) -> tuple[dict, dict] | None:
    """(브랜치, head 커밋). 브랜치가 없으면 None."""
    client = client or get_supabase()
    branch = _one(client.table("artifact_branches").select("*").eq("room_id", room_id).limit(1).execute())
    if not branch:
        return None
    head = _one(client.table("artifact_commits").select("*").eq("id", branch["head_commit_id"]).limit(1).execute())
    return branch, head or {"id": branch["head_commit_id"], "tree": {}}


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_dir(root: Path) -> dict[str, Path]:
    """작업 폴더의 파일. 상대 경로 → 로컬 경로."""
    if not root.is_dir():
        return {}
    return {p.relative_to(root).as_posix(): p for p in sorted(root.rglob("*")) if p.is_file()}


def _list_with_times(client, prefix: str) -> list[tuple[str, str | None]]:
    """prefix 하위 파일의 (키, 수정 시각). 하위 폴더(id 가 None)는 내려간다."""
    out: list[tuple[str, str | None]] = []
    for e in client.storage.from_(_BUCKET).list(prefix) or []:
        name = e.get("name")
        if not name:
            continue
        full = f"{prefix}/{name}"
        if e.get("id") is None:
            out.extend(_list_with_times(client, full))
        else:
            out.append((full, e.get("updated_at") or e.get("created_at")))
    return out


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _room_folder_since(tenant_id: str, room_id: str, since: str | None, client) -> dict[str, dict]:
    """방 작업 폴더(skills/{tenant}/{방}/)에서 since 이후 바뀐 파일을 객체로 올려 tree 항목으로 돌려준다.

    사람이 화면에서 올린 파일은 지금처럼 방 폴더에 먼저 놓인다. 펼칠 때 head 위에 얹어
    에이전트가 그 파일로 일하고, 턴 끝 커밋에 들어간다.
    """
    prefix = f"{_PREFIX}/{tenant_id}/{room_id}"
    try:
        files = _list_with_times(client, prefix)
    except Exception:  # noqa: BLE001
        return {}
    since_dt = _parse_time(since)
    out: dict[str, dict] = {}
    for key, updated in files:
        rel = decode_key_path(key[len(prefix):].lstrip("/"))
        if not rel:
            continue
        updated_dt = _parse_time(updated)
        if since_dt and updated_dt and updated_dt <= since_dt:
            continue
        data = client.storage.from_(_BUCKET).download(key)
        out[rel] = put_object(tenant_id, data, rel, client)
    return out


def materialize(tenant_id: str, room_id: str, dest: Path, client=None) -> BranchState | None:
    """방 브랜치 head 를 작업 폴더로 펼친다. 브랜치가 없으면 None(기존 경로를 쓴다).

    작업 폴더는 head 와 같아진다: 내용이 다른 파일은 받아 쓰고, head 에 없는 파일은 지운다
    (이전 턴의 파일은 턴 끝에 커밋됐으므로 head 가 기준이다). head 이후 방 폴더에 사람이
    올린 파일은 그 위에 얹는다.
    """
    client = client or get_supabase()
    found = get_branch_head(room_id, client)
    if not found:
        return None
    branch, head = found
    tree: dict = dict(head.get("tree") or {})
    work_tree = {**tree, **_room_folder_since(tenant_id, room_id, head.get("created_at"), client)}

    dest.mkdir(parents=True, exist_ok=True)
    local = scan_dir(dest)
    for rel, entry in work_tree.items():
        target = dest / rel
        if rel in local and _sha_file(local[rel]) == entry.get("sha256"):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(read_object(tenant_id, entry["sha256"], client))
    for rel, p in local.items():
        if rel not in work_tree:
            p.unlink(missing_ok=True)
    return BranchState(room_id=room_id, head_commit_id=branch["head_commit_id"], tree=tree)


def build_tree(tenant_id: str, root: Path, known: dict, client=None) -> dict:
    """작업 폴더를 tree 로. known(펼친 tree)에 같은 내용이 있으면 올리지 않는다."""
    client = client or get_supabase()
    tree: dict = {}
    for rel, p in scan_dir(root).items():
        sha = _sha_file(p)
        prev = known.get(rel)
        if prev and prev.get("sha256") == sha:
            tree[rel] = prev
            continue
        tree[rel] = put_object(tenant_id, p.read_bytes(), rel, client)
    return tree


def rebase_changes(base: dict, mine: dict, theirs: dict) -> dict:
    """base → mine 에서 바뀐 경로만 theirs 위에 얹는다."""
    out = dict(theirs)
    for rel, entry in mine.items():
        if (base.get(rel) or {}).get("sha256") != entry.get("sha256"):
            out[rel] = entry
    for rel in base:
        if rel not in mine:
            out.pop(rel, None)
    return out


def _is_retry(exc: Exception) -> bool:
    return getattr(exc, "code", None) == "40001" or "head 가 바뀌었습니다" in str(exc)


def commit_turn(tenant_id: str, state: BranchState, root: Path, reason: str = "agent_turn",
                author_id: str | None = None, client=None) -> dict:
    """작업 폴더의 바뀐 내용을 방 브랜치에 커밋한다. 바뀐 것이 없으면 커밋하지 않는다."""
    client = client or get_supabase()
    mine = build_tree(tenant_id, root, state.tree, client)
    if mine == state.tree:
        return {"commit_id": state.head_commit_id, "created": False}
    expected, theirs = state.head_commit_id, state.tree
    for _ in range(RETRY_LIMIT):
        tree = rebase_changes(state.tree, mine, theirs)
        try:
            res = client.rpc("commit_branch", {
                "p_room_id": state.room_id,
                "p_expected_head": expected,
                "p_tree": tree,
                "p_reason": reason,
                "p_parents": None,
                "p_author_id": author_id,
            }).execute()
            data = getattr(res, "data", res) or {}
            if data.get("created"):
                state.head_commit_id, state.tree = data["commit_id"], tree
            return data
        except Exception as exc:  # noqa: BLE001
            if not _is_retry(exc):
                raise
            branch, head = get_branch_head(state.room_id, client) or (None, None)
            if not branch:
                raise
            expected, theirs = branch["head_commit_id"], dict(head.get("tree") or {})
            logger.info("워크트리 커밋 재시도 | room=%s head=%s", state.room_id, expected)
    raise RuntimeError(f"커밋 재시도 한도를 넘었습니다: {state.room_id}")
