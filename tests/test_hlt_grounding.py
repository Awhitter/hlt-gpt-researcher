from backend.server.hlt_grounding import (
    extract_source_refs,
    merge_report_delivery_receipt,
    prepare_report_delivery,
    prepare_report_record,
    report_is_memory_eligible,
    sanitize_user_visible_research_data,
    source_refs_from_research_sources,
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


def test_code_scoped_delivery_blocks_an_answer_without_validator_backed_sources():
    report = prepare_report_delivery(
        "Marketo is not implemented and onboarding lives in invented/file.ts.",
        {"active_sources": ["codebase", "recruiting"]},
    )

    assert report["deliveryBlocked"] is True
    assert report["verificationStatus"] == "unverified"
    assert "Marketo is not implemented" not in report["answer"]
    assert "couldn't verify" in report["answer"].lower()


def test_code_scoped_delivery_preserves_a_validator_backed_answer():
    answer = "Email capture is implemented in the identity route."
    report = prepare_report_delivery(
        answer,
        {"active_sources": ["codebase"]},
        source_refs=[
            {
                "repo": "Awhitter/nursing-mastery",
                "commitSha": SHA,
                "path": "app/api/profile/route.ts",
                "line": 12,
                "url": URL,
                "exists": True,
            }
        ],
    )

    assert report["deliveryBlocked"] is False
    assert report["verificationStatus"] == "verified"
    assert report["answer"].startswith(answer)
    assert "## Sources" in report["answer"]
    assert URL in report["answer"]


def test_file_read_mcp_payload_becomes_a_visible_validated_source():
    source_url = (
        f"https://github.com/Awhitter/nursing-mastery/blob/{SHA}/"
        "app/(home)/page.tsx#L40-L55"
    )
    sources = [
        {
            "source_type": "mcp",
            "tool_name": "read_source",
            "url": source_url,
            "content": (
                '{"repo":"Awhitter/nursing-mastery","commitSha":"'
                + SHA
                + '","path":"app/(home)/page.tsx","url":"'
                + source_url
                + '","content":"specialty and location"}'
            ),
        }
    ]

    refs = source_refs_from_research_sources(sources)
    assert refs[0]["path"] == "app/(home)/page.tsx"
    assert refs[0]["line"] == 40

    delivered = prepare_report_delivery(
        "The homepage uses nurse specialty and location.",
        {"active_sources": ["codebase"]},
        source_refs=[{**refs[0], "exists": True}],
    )
    assert delivered["deliveryBlocked"] is False
    assert "## Sources" in delivered["answer"]
    assert source_url in delivered["answer"]


def test_search_source_results_do_not_count_as_opened_file_evidence():
    sources = [
        {
            "source_type": "mcp",
            "tool_name": "search_source",
            "url": URL,
            "content": "A broad match that was never opened.",
        }
    ]

    assert source_refs_from_research_sources(sources) == []


def test_verify_only_result_does_not_duplicate_or_replace_opened_file_evidence():
    verify_only = {
        "source_type": "mcp",
        "tool_name": "verify_source_ref",
        "content": (
            '{"repo":"Awhitter/nursing-mastery","commitSha":"'
            + SHA
            + '","path":"app/api/profile/route.ts","exists":true}'
        ),
    }

    assert source_refs_from_research_sources([verify_only]) == []


def test_empty_code_report_does_not_masquerade_as_verified_sources_only():
    delivered = prepare_report_delivery(
        "",
        {"active_sources": ["codebase"]},
        source_refs=[
            {
                "repo": "Awhitter/nursing-mastery",
                "commitSha": SHA,
                "path": "app/api/profile/route.ts",
                "url": URL,
                "exists": True,
            }
        ],
    )

    assert delivered["deliveryBlocked"] is True
    assert delivered["verificationStatus"] == "unverified"
    assert "report writing returned no answer" in delivered["verificationReason"]
    assert "couldn't verify" in delivered["answer"].lower()


def test_mutable_or_short_github_code_link_blocks_code_scoped_delivery():
    mutable = "https://github.com/Awhitter/ScraperVault/blob/7f71718/models/user.py"
    report = prepare_report_delivery(
        f"Definitive source: {mutable}",
        {"active_sources": ["codebase"]},
    )

    assert report["deliveryBlocked"] is True
    assert any("immutable" in claim.lower() for claim in report["unsupportedClaims"])


def test_public_web_report_is_not_subject_to_code_delivery_gate():
    answer = "A public market overview without repository claims."
    report = prepare_report_delivery(answer, {"active_sources": ["firecrawl"]})

    assert report["deliveryBlocked"] is False
    assert report["answer"] == answer


def test_user_visible_log_data_hides_internal_scope_instructions_recursively():
    payload = {
        "output": "Original question\n\nHLT research scope instructions:\n- secret implementation detail",
        "metadata": [
            "A clean subquery",
            "Original question\n\nHLT research scope instructions:\n- more internals",
        ],
    }

    sanitized = sanitize_user_visible_research_data(payload)

    assert sanitized["output"] == "Original question"
    assert sanitized["metadata"] == ["A clean subquery", "Original question"]


def test_private_repo_validation_uses_the_configured_github_mcp_token(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"type":"file"}'

    def open_request(request, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_MCP_TOKEN", "private-repo-token")
    monkeypatch.setattr("backend.server.hlt_grounding.urllib.request.urlopen", open_request)

    report = prepare_report_record({"answer": f"Implemented here: {URL}"}, validate_sources=True)

    assert report["verificationStatus"] == "verified"
    assert captured == {"authorization": "Bearer private-repo-token", "timeout": 6}


def test_private_repo_validation_prefers_the_authenticated_codegraph(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return (
                b'{"repo":"Awhitter/nursing-mastery","commitSha":"'
                + SHA.encode()
                + b'","path":"app/api/profile/route.ts","exists":true,'
                b'"indexedAt":"2026-08-06T00:00:00Z"}'
            )

    def open_request(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["body"] = request.data
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setenv("CODEGRAPH_MCP_URL", "https://codegraph.example/mcp")
    monkeypatch.setenv("CODEGRAPH_MCP_TOKEN", "codegraph-token")
    monkeypatch.setattr("backend.server.hlt_grounding.urllib.request.urlopen", open_request)

    report = prepare_report_record({"answer": f"Implemented here: {URL}"}, validate_sources=True)

    assert report["verificationStatus"] == "verified"
    assert report["sourceRefs"][0]["validationMethod"] == "codegraph"
    assert captured["url"] == "https://codegraph.example/verify-source"
    assert captured["authorization"] == "Bearer codegraph-token"
    assert b'"path": "app/api/profile/route.ts"' in captured["body"]
    assert captured["timeout"] == 6


def test_frontend_upsert_preserves_the_server_source_receipt():
    existing = {
        "id": "run-1",
        "sourceRefs": [
            {
                "repo": "Awhitter/nursing-mastery",
                "commitSha": SHA,
                "path": "app/api/profile/route.ts",
                "exists": True,
            }
        ],
        "verificationStatus": "verified",
        "verificationReason": "Every attached repository source was validated at its exact commit.",
        "unsupportedClaims": [],
        "deliveryBlocked": False,
        "hlt_research_scope": {"active_sources": ["codebase"]},
    }
    frontend_upsert = {
        "id": "run-1",
        "answer": "A richer browser copy.",
        "orderedData": [{"type": "logs", "output": "Research completed"}],
    }

    merged = merge_report_delivery_receipt(existing, frontend_upsert)

    assert merged["sourceRefs"] == existing["sourceRefs"]
    assert merged["verificationStatus"] == "verified"
    assert merged["deliveryBlocked"] is False
    assert merged["hlt_research_scope"] == {"active_sources": ["codebase"]}


def test_report_complete_event_restores_receipt_when_server_record_is_missing():
    frontend_upsert = {
        "id": "run-2",
        "orderedData": [
            {
                "type": "report_complete",
                "metadata": {
                    "sourceRefs": [
                        {
                            "repo": "Awhitter/nursing-mastery",
                            "commitSha": SHA,
                            "path": "app/api/profile/route.ts",
                            "exists": True,
                        }
                    ],
                    "verificationStatus": "verified",
                    "verificationReason": "Validated.",
                    "unsupportedClaims": [],
                    "deliveryBlocked": False,
                    "hlt_research_scope": {"active_sources": ["codebase"]},
                },
            }
        ],
    }

    merged = merge_report_delivery_receipt(None, frontend_upsert)
    report = prepare_report_record(merged)

    assert report["verificationStatus"] == "verified"
    assert report["deliveryBlocked"] is False
    assert report["sourceRefs"][0]["path"] == "app/api/profile/route.ts"


def test_report_complete_event_repairs_a_legacy_overwritten_receipt():
    broken_existing = {
        "id": "run-3",
        "sourceRefs": [],
        "verificationStatus": "unverified",
        "verificationReason": "No validated exact repository source is attached.",
        "unsupportedClaims": [],
    }
    frontend_upsert = {
        "id": "run-3",
        "orderedData": [
            {
                "type": "report_complete",
                "metadata": {
                    "sourceRefs": [
                        {
                            "repo": "Awhitter/nursing-mastery",
                            "commitSha": SHA,
                            "path": "app/api/profile/route.ts",
                            "exists": True,
                        }
                    ],
                    "verificationStatus": "verified",
                    "unsupportedClaims": [],
                    "deliveryBlocked": False,
                    "hlt_research_scope": {"active_sources": ["codebase"]},
                },
            }
        ],
    }

    merged = merge_report_delivery_receipt(broken_existing, frontend_upsert)

    assert merged["sourceRefs"][0]["path"] == "app/api/profile/route.ts"
    assert merged["verificationStatus"] == "verified"
    assert merged["hlt_research_scope"] == {"active_sources": ["codebase"]}
