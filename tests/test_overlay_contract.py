"""Overlay contract: docking stamps, leaf import ban, Monday diff grades."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = REPO_ROOT / "scripts" / "check_overlay_contract.py"


def load_checker():
    spec = importlib.util.spec_from_file_location("check_overlay_contract", CHECKER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return load_checker()


@pytest.fixture(scope="module")
def manifest(checker):
    return checker.load_manifest(REPO_ROOT / "overlay" / "manifest.yaml")


def test_current_checkout_satisfies_overlay_contract(checker, manifest):
    failures = checker.check_repo(REPO_ROOT, manifest)
    assert failures == []


def test_missing_install_hook_is_reported(checker):
    text = "from fastapi import FastAPI\n\napp = FastAPI()\n"
    missing = checker.missing_markers(text, ["_install_hlt_extensions(app)"])
    assert missing == ["_install_hlt_extensions(app)"]


def test_leaf_relative_import_is_detected(checker):
    source = "from .hlt_extensions import install\n"
    assert checker.hlt_extensions_import_lines(source) == [1]


def test_commented_leaf_import_is_ignored(checker):
    source = "# from .hlt_extensions import install\nprint('ok')\n"
    assert checker.hlt_extensions_import_lines(source) == []


def test_conflict_markers_are_detected(checker):
    assert checker.has_conflict_markers("<<<<<<< HEAD\nkeep\n>>>>>>>\n")
    assert not checker.has_conflict_markers("normal file\n")


def test_diff_grade_classifies_docking_owned_and_other(checker, manifest):
    assert checker.classify_path("backend/server/app.py", manifest) == "docking"
    assert checker.classify_path("backend/server/hlt_brain.py", manifest) == "owned"
    assert checker.classify_path("gpt_researcher/retrievers/firecrawl/firecrawl_search.py", manifest) == "owned"
    assert checker.classify_path("gpt_researcher/agent.py", manifest) == "other"


def test_check_repo_fails_when_a_leaf_imports_the_router(checker, manifest, tmp_path):
    """Copy the real tree pointers via a tiny fixture checkout."""
    leaf = tmp_path / "backend" / "server" / "hlt_brain.py"
    leaf.parent.mkdir(parents=True)
    leaf.write_text("from .hlt_extensions import install\n", encoding="utf-8")
    failures = checker.check_repo(tmp_path, {"leaves": ["backend/server/hlt_brain.py"], "owned": [], "docking": []})
    assert any("leaf imports hlt_extensions" in item for item in failures)


def test_check_repo_fails_when_install_stamp_is_gone(checker, tmp_path):
    app = tmp_path / "backend" / "server" / "app.py"
    app.parent.mkdir(parents=True)
    app.write_text("app = FastAPI()\n", encoding="utf-8")
    manifest = {
        "owned": [],
        "leaves": [],
        "docking": [
            {
                "path": "backend/server/app.py",
                "markers": ["_install_hlt_extensions(app)"],
            }
        ],
    }
    failures = checker.check_repo(tmp_path, manifest)
    assert any("missing docking marker" in item for item in failures)
