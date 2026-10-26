"""Keep source tracking shared across research sections without provider calls.

Backported from upstream PR #2122, with a sibling-section mutation regression.
"""

import gpt_researcher.agent as agent_module


class _FakeConfig:
    prompt_family = "default"
    embedding_provider = "test"
    embedding_model = "test"
    embedding_kwargs = {}
    mcp_strategy = "fast"

    def __init__(self, _config_path=None):
        pass

    def set_verbose(self, _verbose):
        pass


def _stub_constructor_dependencies(monkeypatch):
    monkeypatch.setattr(agent_module, "Config", _FakeConfig)
    monkeypatch.setattr(agent_module, "get_prompt_family", lambda *_args: object())
    monkeypatch.setattr(agent_module, "get_retrievers", lambda *_args: [])
    monkeypatch.setattr(agent_module, "Memory", lambda *_args, **_kwargs: object())

    for name in (
        "ResearchConductor",
        "ReportGenerator",
        "ContextManager",
        "BrowserManager",
        "SourceCurator",
        "ImageGenerator",
    ):
        monkeypatch.setattr(agent_module, name, lambda *_args, **_kwargs: object())


def test_preserves_empty_visited_urls_identity(monkeypatch):
    _stub_constructor_dependencies(monkeypatch)
    shared = set()
    researcher = agent_module.GPTResearcher(query="test", visited_urls=shared, verbose=False)
    assert researcher.visited_urls is shared


def test_preserves_populated_visited_urls_identity(monkeypatch):
    _stub_constructor_dependencies(monkeypatch)
    shared = {"https://example.com"}
    researcher = agent_module.GPTResearcher(query="test", visited_urls=shared, verbose=False)
    assert researcher.visited_urls is shared


def test_sibling_sections_observe_each_others_visited_urls(monkeypatch):
    _stub_constructor_dependencies(monkeypatch)
    shared = set()
    first = agent_module.GPTResearcher(query="first", visited_urls=shared, verbose=False)
    second = agent_module.GPTResearcher(query="second", visited_urls=shared, verbose=False)

    first.visited_urls.add("https://example.com/first")
    second.visited_urls.add("https://example.com/second")

    assert first.visited_urls == second.visited_urls == shared == {
        "https://example.com/first",
        "https://example.com/second",
    }


def test_default_visited_urls_creates_fresh_set(monkeypatch):
    _stub_constructor_dependencies(monkeypatch)
    first = agent_module.GPTResearcher(query="first", visited_urls=None, verbose=False)
    second = agent_module.GPTResearcher(query="second", verbose=False)

    first.visited_urls.add("https://example.com/first")

    assert second.visited_urls == set()
    assert first.visited_urls is not second.visited_urls
