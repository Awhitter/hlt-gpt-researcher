#!/usr/bin/env python3
"""Optional, zero-dependency prebuilt `.gitnexus` graph tarball sync.

Used only when `CODEGRAPH_PREBUILT_GRAPH_URL_TEMPLATE` is configured (for example,
pulling a weekly Ubicloud-built graph archive for large monolith repos while running
on a 2 GB Render plan). Standard library only.

Security & safety invariants:
- Reads URL template and optional bearer from environment (never exposed in argv).
- Accepts only `https://` URLs (`http://` allowed only for localhost / 127.0.0.1 / [::1])
  with no embedded credentials (`user:pass@`).
- Refuses HTTP redirects so optional bearer credentials never travel to another origin.
- Streams download directly to a temporary file (never double-buffering in RAM) under
  both a wall-clock deadline and a compressed byte ceiling (`CODEGRAPH_PREBUILT_GRAPH_MAX_MB`).
- Enforces uncompressed byte and member-count limits (preventing tar/gzip bombs) and
  validates every archive member: regular files and directories strictly under `.gitnexus/`
  (no control chars, absolute paths, `..`, symlinks, hardlinks, or devices).
- Stages extraction in `.gitnexus.staging.<pid>` and atomically swaps into `.gitnexus`,
  preserving any existing `.gitnexus` intact on error.
"""
from __future__ import annotations

import argparse
import http.client
import io
import os
import posixpath
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_MB = 512
DEFAULT_MAX_EXTRACTED_MULTIPLIER = 4
DEFAULT_MAX_MEMBERS = 50_000
_SLUG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        fp.close()
        raise urllib.error.HTTPError("https://prebuilt-graph/archive", code, "redirect refused", headers, None)


def validate_url(url: str) -> str | None:
    """Return error message if URL is unsafe, else None."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
        parts.port  # noqa: B018
    except ValueError:
        return "prebuilt graph URL is invalid"
    host = (parts.hostname or "").lower()
    if not host:
        return "prebuilt graph URL has no host"
    if parts.username or parts.password:
        return "prebuilt graph URL must not contain embedded credentials"
    if parts.scheme == "https" or (parts.scheme == "http" and host in LOCAL_HOSTS):
        return None
    return "prebuilt graph URL must use https (plain http only for localhost, 127.0.0.1, [::1])"


def render_url(template: str, slug: str) -> str:
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError("unsafe repository slug")
    return template.strip().replace("{slug}", urllib.parse.quote(slug, safe=""))


def _safe_member_relpath(raw_name: str) -> str | None:
    """Return normalized relative path inside `.gitnexus/`, or None if unsafe."""
    if not raw_name or _CONTROL_RE.search(raw_name):
        return None
    if raw_name.startswith("/") or "\\" in raw_name:
        return None
    name = raw_name
    while name.startswith("./"):
        name = name[2:]
    normalized = posixpath.normpath(name)
    if normalized in {"", ".", ".."} or normalized.startswith("../") or normalized.startswith("/"):
        return None
    if normalized == ".gitnexus":
        return "."
    if normalized.startswith(".gitnexus/"):
        return normalized[len(".gitnexus/"):]
    return None


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _extract_tar(
    tar: tarfile.TarFile,
    dest_repo: Path,
    *,
    max_extracted_bytes: int,
    max_members: int,
) -> int:
    if not dest_repo.is_dir() or dest_repo.is_symlink():
        raise ValueError("destination repository directory is missing or unsafe")

    staging = dest_repo / f".gitnexus.staging.{os.getpid()}"
    target = dest_repo / ".gitnexus"
    backup = dest_repo / f".gitnexus.prev.{os.getpid()}"
    if target.is_symlink():
        raise ValueError("existing .gitnexus is a symlink")

    _remove_path(staging)
    staging.mkdir(mode=0o755, parents=False, exist_ok=False)
    resolved_staging = staging.resolve()
    files_extracted = 0
    total_extracted = 0
    member_count = 0
    try:
        for member in tar:
            member_count += 1
            if member_count > max_members:
                raise ValueError(f"prebuilt graph archive exceeded {max_members} member limit")
            if not (member.isfile() or member.isdir()):
                raise ValueError("refusing non-regular tar member")
            rel = _safe_member_relpath(member.name)
            if rel is None or (member.isfile() and rel == "."):
                raise ValueError("refusing tar member outside .gitnexus/")
            out_path = staging if rel == "." else staging / rel
            if not out_path.resolve().is_relative_to(resolved_staging):
                raise ValueError("tar member escaped staging directory")
            if member.isdir():
                out_path.mkdir(parents=True, exist_ok=True)
            else:
                if member.size < 0 or total_extracted + member.size > max_extracted_bytes:
                    raise ValueError("prebuilt graph archive exceeded extracted size limit")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(member)
                if src is None:
                    raise ValueError("could not read tar member")
                with src, out_path.open("wb") as dst:
                    while True:
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        total_extracted += len(chunk)
                        if total_extracted > max_extracted_bytes:
                            raise ValueError("prebuilt graph archive exceeded extracted size limit")
                        dst.write(chunk)
                files_extracted += 1
        if files_extracted == 0:
            raise ValueError("prebuilt graph archive contained no files under .gitnexus/")

        _remove_path(backup)
        had_target = target.exists()
        if had_target:
            target.rename(backup)
        try:
            staging.rename(target)
        except OSError:
            if had_target and backup.exists() and not target.exists():
                backup.rename(target)
            raise
        if had_target and backup.exists():
            _remove_path(backup)
        return files_extracted
    except Exception:
        _remove_path(staging)
        raise


def extract_safe_gitnexus_tarball(
    archive_bytes: bytes,
    dest_repo: Path,
    *,
    max_extracted_bytes: int = DEFAULT_MAX_MB * DEFAULT_MAX_EXTRACTED_MULTIPLIER * 1024 * 1024,
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> int:
    """Safely extract an in-memory `.gitnexus` tar(.gz) archive into `dest_repo/.gitnexus`."""
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tar:
        return _extract_tar(
            tar,
            dest_repo,
            max_extracted_bytes=max_extracted_bytes,
            max_members=max_members,
        )


def download_and_install(
    slug: str,
    dest_repo: Path,
    url_template: str,
    *,
    token: str | None = None,
    max_mb: int = DEFAULT_MAX_MB,
    timeout: float = DEFAULT_TIMEOUT_S,
    opener: urllib.request.OpenerDirector | None = None,
) -> int:
    url = render_url(url_template, slug)
    problem = validate_url(url)
    if problem:
        raise ValueError(problem)
    if not dest_repo.is_dir() or dest_repo.is_symlink():
        raise ValueError("destination repository directory is missing or unsafe")
    headers = {"User-Agent": "hlt-codegraph-reindex/1"}
    clean_token = (token or "").strip()
    if clean_token:
        if re.search(r"[^\x21-\x7e]", clean_token):
            raise ValueError("CODEGRAPH_PREBUILT_GRAPH_TOKEN is not a usable bearer header")
        headers["Authorization"] = f"Bearer {clean_token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    client = opener if opener is not None else urllib.request.build_opener(_NoRedirects)
    max_bytes = max(1, int(max_mb)) * 1024 * 1024
    max_extracted_bytes = max_bytes * DEFAULT_MAX_EXTRACTED_MULTIPLIER
    deadline = time.monotonic() + max(0.1, float(timeout))
    tmp_archive = dest_repo / f".gitnexus.download.{os.getpid()}.tar"
    try:
        total = 0
        with client.open(req, timeout=max(0.1, deadline - time.monotonic())) as response, tmp_archive.open("wb") as out:
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("prebuilt graph download timed out")
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"prebuilt graph archive exceeded {max_mb} MB limit")
                out.write(chunk)
        with tarfile.open(name=tmp_archive, mode="r:*") as tar:
            return _extract_tar(
                tar,
                dest_repo,
                max_extracted_bytes=max_extracted_bytes,
                max_members=DEFAULT_MAX_MEMBERS,
            )
    finally:
        tmp_archive.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download and safely install a prebuilt .gitnexus tarball.")
    parser.add_argument("--slug", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--url-template", default=None)
    args = parser.parse_args(argv)

    url_template = (args.url_template or os.getenv("CODEGRAPH_PREBUILT_GRAPH_URL_TEMPLATE") or "").strip()
    if not url_template:
        print(f"[codegraph] {args.slug}: prebuilt graph sync skipped (no URL template set)", file=sys.stderr)
        return 1

    raw_max = (os.getenv("CODEGRAPH_PREBUILT_GRAPH_MAX_MB") or str(DEFAULT_MAX_MB)).strip()
    max_mb = int(raw_max) if raw_max.isdigit() and int(raw_max) > 0 else DEFAULT_MAX_MB
    token = os.getenv("CODEGRAPH_PREBUILT_GRAPH_TOKEN")
    try:
        count = download_and_install(
            args.slug,
            Path(args.dest),
            url_template,
            token=token,
            max_mb=max_mb,
        )
    except urllib.error.HTTPError as error:
        print(f"[codegraph] {args.slug}: prebuilt graph sync skipped (HTTP {error.code})", file=sys.stderr)
        return 1
    except TimeoutError:
        print(f"[codegraph] {args.slug}: prebuilt graph sync skipped (download timed out)", file=sys.stderr)
        return 1
    except (urllib.error.URLError, http.client.HTTPException, OSError):
        # Never log raw urllib/OS error text: it can echo a presigned query string.
        print(f"[codegraph] {args.slug}: prebuilt graph sync skipped (delivery failed)", file=sys.stderr)
        return 1
    except (ValueError, tarfile.TarError) as error:
        print(f"[codegraph] {args.slug}: prebuilt graph sync skipped ({str(error)[:140]})", file=sys.stderr)
        return 1
    print(f"[codegraph] {args.slug}: installed prebuilt structural graph ({count} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
