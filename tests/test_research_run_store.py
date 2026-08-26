import sqlite3

from gpt_researcher.research_run_store import (
    INTERRUPTED_ERROR_CODE,
    ResearchRunStore,
)


def test_research_run_store_migrates_and_round_trips_json(tmp_path):
    db_path = tmp_path / "runs.sqlite3"
    store = ResearchRunStore(db_path)

    with sqlite3.connect(db_path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 5

    store.create_run(
        "run-1",
        query="durable research",
        report_type="research_report",
        report_source="web",
        tone="Objective",
        resource_topic="durable research",
        hlt_research_scope={"active_sources": ["codebase"]},
        source_policy={"version": "source_policy.v1", "enforcement": "strict"},
    )
    store.complete_run(
        "run-1",
        context=[{"finding": "sqlite survives restart"}],
        sources=[{"title": "Source", "url": "https://example.com", "content": "abc"}],
        source_urls=["https://example.com"],
        research_images=[
            {
                "url": "https://example.com/alec.jpg",
                "source_url": "https://example.com/profile",
                "alt_text": "Alec Whitters",
            }
        ],
        costs=0.12,
        report_path="outputs/run-1.md",
        md_path="outputs/run-1.md",
        hlt_research_scope={"active_sources": ["codebase"], "degraded_sources": []},
        source_manifest={"version": "source_manifest.v1", "status": "passed"},
        report_quality={"version": "report_quality.v1", "status": "passed"},
    )

    reopened = ResearchRunStore(db_path)
    run = reopened.get_run("run-1")

    assert run["status"] == "completed"
    assert run["context"] == [{"finding": "sqlite survives restart"}]
    assert run["sources"][0]["title"] == "Source"
    assert run["source_urls"] == ["https://example.com"]
    assert run["source_count"] == 1
    assert run["research_images"][0]["source_url"] == "https://example.com/profile"
    assert run["costs"] == 0.12
    assert run["hlt_research_scope"]["active_sources"] == ["codebase"]
    assert run["source_policy"]["enforcement"] == "strict"
    assert run["source_manifest"]["status"] == "passed"
    assert run["report_quality"]["status"] == "passed"
    assert reopened.get_run_by_resource_topic("durable research")["research_id"] == "run-1"


def test_research_run_store_marks_running_rows_interrupted_on_startup(tmp_path):
    db_path = tmp_path / "runs.sqlite3"
    store = ResearchRunStore(db_path, recover_interrupted=False)
    store.create_run("run-2", query="unfinished", status="running")

    recovered = ResearchRunStore(db_path, recover_interrupted=True)
    run = recovered.get_run("run-2")

    assert run["status"] == "failed"
    assert run["error_code"] == INTERRUPTED_ERROR_CODE
    assert "restart" in run["error_message"]


def test_v3_database_migrates_without_losing_existing_run(tmp_path):
    db_path = tmp_path / "v3-runs.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE research_runs (
                research_id TEXT PRIMARY KEY,
                query TEXT NOT NULL,
                report_type TEXT,
                report_source TEXT,
                tone TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                context_json TEXT,
                sources_json TEXT,
                source_urls_json TEXT,
                source_count INTEGER NOT NULL DEFAULT 0,
                costs REAL NOT NULL DEFAULT 0,
                report_path TEXT,
                md_path TEXT,
                pdf_path TEXT,
                docx_path TEXT,
                error_code TEXT,
                error_message TEXT,
                resource_topic TEXT,
                hlt_research_scope_json TEXT,
                research_images_json TEXT
            );
            INSERT INTO research_runs (
                research_id, query, status, created_at, updated_at,
                context_json, sources_json, source_urls_json, source_count,
                costs, research_images_json
            ) VALUES (
                'legacy-v3', 'legacy query', 'completed', '2026-01-01',
                '2026-01-01', '[{"finding":"preserved"}]',
                '[{"title":"Legacy","url":"https://example.com"}]',
                '["https://example.com"]', 1, 0.5, '[]'
            );
            PRAGMA user_version = 3;
            """
        )

    store = ResearchRunStore(db_path, recover_interrupted=False)
    run = store.get_run("legacy-v3")
    with sqlite3.connect(db_path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]

    assert version == 5
    assert run["query"] == "legacy query"
    assert run["context"] == [{"finding": "preserved"}]
    assert run["sources"][0]["title"] == "Legacy"
    assert run["source_policy"] is None
    assert run["source_manifest"] is None
    assert run["report_quality"] is None
    assert run["rejected_report_quality"] is None


def test_fail_run_persists_receipt_and_terminal_state_in_one_update(
    monkeypatch, tmp_path
):
    store = ResearchRunStore(tmp_path / "atomic.sqlite3", recover_interrupted=False)
    store.create_run("atomic-run", query="atomic failure")
    original_update = store.update_run
    updates = []

    def spy_update(research_id, **fields):
        updates.append((research_id, fields.copy()))
        return original_update(research_id, **fields)

    monkeypatch.setattr(store, "update_run", spy_update)
    store.fail_run(
        "atomic-run",
        error_code="source_manifest_failed",
        error_message="manifest failed",
        source_manifest={"status": "failed"},
        sources=[],
        costs=0.25,
    )

    assert len(updates) == 1
    fields = updates[0][1]
    assert fields["status"] == "failed"
    assert fields["error_code"] == "source_manifest_failed"
    assert fields["source_manifest"] == {"status": "failed"}
    run = store.get_run("atomic-run")
    assert run["status"] == "failed"
    assert run["source_manifest"] == {"status": "failed"}
    assert run["costs"] == 0.25
