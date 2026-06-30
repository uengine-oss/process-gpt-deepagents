"""Utilities for uploading skills from compressed archives or Git repositories."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

_ALLOWED_EXTENSIONS = {".skill", ".zip"}
_ALLOWED_SCHEMES = {"https", "http", "git"}
_DEFAULT_CLONE_TIMEOUT = 60  # seconds


def check_extension(filename: str) -> str:
    """Return the lowercased extension if allowed, raise ValueError otherwise."""
    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file extension '{ext}'. Allowed: {sorted(_ALLOWED_EXTENSIONS)}")
    return ext


def get_max_upload_bytes() -> int:
    try:
        mb = int(os.getenv("SKILL_UPLOAD_MAX_MB", "100"))
    except ValueError:
        mb = 100
    return mb * 1024 * 1024


def check_size(size: int) -> None:
    limit = get_max_upload_bytes()
    if size > limit:
        raise FileTooLargeError(size, limit)


class FileTooLargeError(Exception):
    def __init__(self, size: int, limit: int):
        self.size = size
        self.limit = limit
        super().__init__(f"File size {size} exceeds limit {limit}")


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract zip contents to dest, blocking path traversal."""
    dest_resolved = dest.resolve()
    for member in zf.infolist():
        target = (dest / member.filename).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError:
            continue  # silently skip traversal attempts
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def extract_archive(archive_bytes: bytes, dest: Path) -> None:
    """Extract a zip/skill archive to dest with path traversal protection."""
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as zf:
            _safe_extract(zf, dest)
    except zipfile.BadZipFile as e:
        raise ValueError(f"Invalid archive: {e}") from e


_JUNK_DIR_NAMES = {"__MACOSX"}


def _is_junk_part(name: str) -> bool:
    return name in _JUNK_DIR_NAMES or name.startswith(".")


def find_skill_root(extracted_dir: Path) -> Path | None:
    """Return the directory containing SKILL.md, or None if not found.

    Searches the extracted tree at any depth for a SKILL.md, ignoring
    archive junk such as macOS Finder's __MACOSX folder or dotfiles/dirs
    (.DS_Store, .git, ...). Returns the unique parent directory if exactly
    one candidate is found; returns None if there is none or the location
    is ambiguous (multiple SKILL.md files).
    """
    candidates = sorted(
        {
            p.parent
            for p in extracted_dir.rglob("SKILL.md")
            if not any(_is_junk_part(part) for part in p.relative_to(extracted_dir).parts[:-1])
        },
        key=lambda p: len(p.relative_to(extracted_dir).parts),
    )
    if len(candidates) == 1:
        return candidates[0]
    return None


def skill_name_from_filename(filename: str) -> str:
    """Derive skill name from an archive filename (strips extension)."""
    return Path(filename).stem


def validate_url_scheme(url: str) -> None:
    """Raise ValueError if the URL scheme is not in _ALLOWED_SCHEMES."""
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+\-.]*):\/\/", url.strip())
    if not m or m.group(1).lower() not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"URL scheme not allowed. Allowed: {sorted(_ALLOWED_SCHEMES)}. Got: {url!r}"
        )


def skill_name_from_url(url: str) -> str:
    """Extract skill name from the last path segment of a Git URL (strips .git)."""
    path = url.rstrip("/").split("?")[0].split("#")[0]
    last = path.rsplit("/", 1)[-1]
    if last.lower().endswith(".git"):
        last = last[:-4]
    return last or "skill"


def clone_skill_repo(url: str, dest: Path, timeout: int = _DEFAULT_CLONE_TIMEOUT) -> None:
    """Shallow-clone a Git repository into dest.

    Raises:
        subprocess.TimeoutExpired: if clone exceeds timeout seconds
        RuntimeError: if git clone exits with a non-zero code
    """
    try:
        result = subprocess.run(
            ["git", "clone", "--depth=1", url, str(dest)],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        shutil.rmtree(dest, ignore_errors=True)
        raise

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"git clone failed: {stderr}")


def list_skill_files(skill_dir: Path) -> list[str]:
    """Return relative paths of all files under skill_dir (excluding .git)."""
    return [
        str(p.relative_to(skill_dir))
        for p in sorted(skill_dir.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    ]


def get_tenant_skill_dest(tenant_id: str, skill_name: str, config_id: str = "local") -> Path:
    """Return the canonical destination path for a tenant skill.

    config_id scopes the path to a specific git provider config (tenant_git_config.id)
    or "local" for skills uploaded via ZIP / external git URL.
    """
    _BASE_DIR = Path(__file__).parent.parent.parent
    raw = os.getenv("SKILLS_DIRS", str(_BASE_DIR / "skills"))
    first = raw.split(",")[0].strip() or str(_BASE_DIR / "skills")
    root = Path(first) if os.path.isabs(first) else (_BASE_DIR / first).resolve()
    return root / tenant_id / config_id / skill_name
