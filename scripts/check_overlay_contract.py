#!/usr/bin/env python3
"""Grade the HLT overlay contract against this checkout.

Fails when a docking stamp is missing, a leaf imports hlt_extensions, or
conflict markers ate an overlay/docking file. Optional --diff-base classifies
changed paths so Monday sync PRs can rank attention.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs pyyaml before invoking
    sys.exit("pyyaml is required: pip install pyyaml")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "overlay" / "manifest.yaml"
CONFLICT_MARKERS = ("<<<<<<<", ">>>>>>>")
SKIP_DIR_NAMES = {".git", "__pycache__", "node_modules", ".next"}


def load_manifest(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"overlay manifest must be a mapping: {path}")
    return data


def missing_markers(text: str, markers: Iterable[str]) -> list[str]:
    return [marker for marker in markers if marker not in text]


def has_conflict_markers(text: str) -> bool:
    return any(marker in text for marker in CONFLICT_MARKERS)


def hlt_extensions_import_lines(source: str, filename: str = "<leaf>") -> list[int]:
    """Return 1-based line numbers of real hlt_extensions imports (not comments)."""

    tree = ast.parse(source, filename=filename)
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[-1] == "hlt_extensions" for alias in node.names):
                hits.append(getattr(node, "lineno", 0))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            from_extensions = module.split(".")[-1] == "hlt_extensions" if module else False
            imported_name = any(alias.name == "hlt_extensions" for alias in node.names)
            if from_extensions or imported_name:
                hits.append(getattr(node, "lineno", 0))
    return sorted({line for line in hits if line})


def _expand_pattern(root: Path, pattern: str) -> list[Path]:
    rel = pattern.rstrip("/")
    candidate = root / rel
    if candidate.is_file():
        return [candidate]
    if candidate.is_dir():
        files: list[Path] = []
        for path in candidate.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            files.append(path)
        return sorted(files)
    matched = sorted(p for p in root.glob(rel) if p.is_file())
    return matched


def owned_files(root: Path, manifest: dict[str, Any]) -> dict[str, list[Path]]:
    mapping: dict[str, list[Path]] = {}
    for pattern in manifest.get("owned") or []:
        mapping[pattern] = _expand_pattern(root, str(pattern))
    return mapping


def classify_path(rel_path: str, manifest: dict[str, Any], root: Path | None = None) -> str:
    normalized = rel_path.replace("\\", "/")
    base = root or REPO_ROOT
    for entry in manifest.get("docking") or []:
        if str(entry["path"]) == normalized:
            return "docking"
    for pattern in manifest.get("owned") or []:
        matches = _expand_pattern(base, str(pattern))
        rels = {path.relative_to(base).as_posix() for path in matches}
        if normalized in rels:
            return "owned"
        prefix = str(pattern).rstrip("/")
        if prefix.endswith("/") or (base / prefix).is_dir():
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return "owned"
        if "*" in prefix and fnmatch.fnmatch(normalized, prefix):
            return "owned"
    return "other"


def changed_paths(root: Path, diff_base: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", diff_base],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git diff failed against {diff_base}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def check_repo(root: Path, manifest: dict[str, Any]) -> list[str]:
    failures: list[str] = []

    for pattern, files in owned_files(root, manifest).items():
        if not files:
            failures.append(f"owned pattern matched nothing: {pattern}")

    for entry in manifest.get("docking") or []:
        path = root / str(entry["path"])
        if not path.is_file():
            failures.append(f"missing docking file: {entry['path']}")
            continue
        text = path.read_text(encoding="utf-8")
        if has_conflict_markers(text):
            failures.append(f"conflict markers in docking file: {entry['path']}")
        for marker in missing_markers(text, entry.get("markers") or []):
            failures.append(f"missing docking marker in {entry['path']}: {marker}")

    for rel in manifest.get("leaves") or []:
        path = root / str(rel)
        if not path.is_file():
            failures.append(f"missing leaf module: {rel}")
            continue
        text = path.read_text(encoding="utf-8")
        if has_conflict_markers(text):
            failures.append(f"conflict markers in leaf: {rel}")
        try:
            lines = hlt_extensions_import_lines(text, filename=str(rel))
        except SyntaxError as error:
            failures.append(f"cannot parse leaf {rel}: {error}")
            continue
        for line in lines:
            failures.append(f"leaf imports hlt_extensions: {rel}:{line}")

    return failures


def render_report(
    *,
    failures: list[str],
    graded: dict[str, list[str]] | None,
) -> str:
    result = "fail" if failures else "pass"
    lines = [
        "## Overlay contract",
        "",
        f"**Result:** {result}",
        "",
    ]
    if failures:
        lines.append("### Failures")
        lines.extend(f"- {item}" for item in failures)
        lines.append("")
    if graded is not None:
        docking = graded.get("docking") or []
        owned = graded.get("owned") or []
        other = graded.get("other") or []
        lines.append("### Docking patches in this diff")
        if docking:
            lines.extend(f"- `{path}`" for path in docking)
        else:
            lines.append("- none")
        lines.append("")
        lines.append("### Owned overlay in this diff")
        if owned:
            lines.extend(f"- `{path}`" for path in owned)
        else:
            lines.append("- none")
        lines.append("")
        lines.append("### Other files")
        lines.append(f"- {len(other)} file(s) (informational)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--diff-base", default="")
    parser.add_argument("--write-report", type=Path)
    args = parser.parse_args(argv)

    root = args.root.resolve()
    manifest = load_manifest(args.manifest if args.manifest.is_absolute() else root / args.manifest)
    failures = check_repo(root, manifest)

    graded = None
    if args.diff_base:
        try:
            paths = changed_paths(root, args.diff_base)
        except RuntimeError as error:
            failures.append(str(error))
            paths = []
        graded = {"docking": [], "owned": [], "other": []}
        for rel in paths:
            graded[classify_path(rel, manifest, root)].append(rel)

    report = render_report(failures=failures, graded=graded)
    sys.stdout.write(report)
    if args.write_report:
        args.write_report.write_text(report, encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
