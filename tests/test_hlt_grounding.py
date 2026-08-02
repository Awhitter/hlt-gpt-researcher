from backend.server.hlt_grounding import (
    extract_source_refs,
    prepare_report_record,
    report_is_memory_eligible,
)


SHA = "a" * 40
URL = f"https://github.com/Awhitter/nursing-mastery/blob/{SHA}/app/api/profile/route.ts#L12-L18"


def test_legacy_answer_is_retained_but_quarantined():
    report = prepare_report_record({"id": "legacy", "answer": "A useful old answer."})
    assert report["answer"] == "A useful old answer."
    assert report["verificationStatus"] == "unverified"
    assert report_is_memory_eligible(report) is False


def test_immutable_link_is_partial_until_validator_proves_path():
    report = prepare_report_record({"answer": f"Implemented here: {URL}"})
    assert report["verificationStatus"] == "partial"
    assert report["sourceRefs"][0]["path"] == "app/api/profile/route.ts"
    assert report["sourceRefs"][0]["line"] == 12


def test_all_validator_backed_sources_make_report_verified():
    report = prepare_report_record(
        {
            "answer": "The profile route captures the field.",
            "sourceRefs": [
                {
                    "repo": "Awhitter/nursing-mastery",
                    "commitSha": SHA,
                    "path": "app/api/profile/route.ts",
                    "line": 12,
                    "url": URL,
                    "indexedAt": "2026-08-02T12:00:00Z",
                    "exists": True,
                }
            ],
        }
    )
    assert report["verificationStatus"] == "verified"
    assert report["sourceFreshness"] == "2026-08-02T12:00:00Z"
    assert report_is_memory_eligible(report) is True


def test_one_unvalidated_source_keeps_report_partial():
    report = prepare_report_record(
        {
            "sourceRefs": [
                {"repo": "Awhitter/nursing-mastery", "commitSha": SHA, "path": "one.ts", "exists": True},
                {"repo": "Awhitter/nursing-mastery", "commitSha": SHA, "path": "two.ts"},
            ]
        }
    )
    assert report["verificationStatus"] == "partial"


def test_missing_validated_path_becomes_unsupported_claim():
    report = prepare_report_record(
        {
            "sourceRefs": [
                {"repo": "Awhitter/ScraperVault", "commitSha": SHA, "path": "invented/queue.py", "exists": False}
            ]
        }
    )
    assert report["verificationStatus"] == "partial"
    assert "invented/queue.py" in report["unsupportedClaims"][0]


def test_source_extraction_rejects_branch_links():
    assert extract_source_refs("https://github.com/Awhitter/repo/blob/main/file.ts") == []
