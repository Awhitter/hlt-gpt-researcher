import asyncio
from types import SimpleNamespace

from bs4 import BeautifulSoup

import gpt_researcher.skills.image_generator as image_skill
import gpt_researcher.skills.browser as browser_skill
from gpt_researcher.scraper.utils import get_relevant_images, is_likely_content_image
from gpt_researcher.skills.browser import BrowserManager


def test_relevant_images_include_attributed_open_graph_image_first():
    soup = BeautifulSoup(
        """
        <html><head>
          <meta property="og:image" content="/portrait.jpg">
          <meta property="og:image:alt" content="Portrait of Alec Whitters">
        </head><body>
          <img src="/logo.png" width="120" height="40" alt="Logo">
        </body></html>
        """,
        "html.parser",
    )

    images = get_relevant_images(soup, "https://example.com/alec")

    assert images == [
        {
            "url": "https://example.com/portrait.jpg",
            "score": 5,
            "source_url": "https://example.com/alec",
            "alt_text": "Portrait of Alec Whitters",
        }
    ]


def test_relevant_images_keep_source_and_alt_metadata_for_page_images():
    soup = BeautifulSoup(
        '<img class="hero" src="/team.jpg" alt="HLT leadership team">',
        "html.parser",
    )

    images = get_relevant_images(soup, "https://example.com/about")

    assert images[0] == {
        "url": "https://example.com/team.jpg",
        "score": 4,
        "source_url": "https://example.com/about",
        "alt_text": "HLT leadership team",
    }


def test_generated_image_planner_can_decline_and_forbids_real_person_likeness(monkeypatch):
    captured = {}

    async def fake_completion(**kwargs):
        captured.update(kwargs)
        return "[]"

    monkeypatch.setattr(image_skill, "create_chat_completion", fake_completion)
    researcher = SimpleNamespace(
        cfg=SimpleNamespace(
            image_generation_enabled=False,
            image_generation_max_images=2,
            fast_llm_model="fake",
            fast_llm_provider="fake",
            llm_kwargs={},
        ),
        add_costs=lambda *_args, **_kwargs: None,
    )
    generator = image_skill.ImageGenerator(researcher)

    concepts = asyncio.run(
        generator._plan_image_concepts("A biographical research packet", "Who is Alec Whitters?")
    )

    prompt = captured["messages"][1]["content"]
    assert concepts == []
    assert "Return 0-2" in prompt
    assert "Never synthesize a portrait" in prompt


def test_browser_selection_preserves_image_attribution():
    researcher = SimpleNamespace(
        cfg=SimpleNamespace(max_scraper_workers=1, scraper_rate_limit_delay=0),
        get_research_images=lambda: [],
    )
    manager = BrowserManager(researcher)

    selected = manager.select_top_images(
        [
            {
                "url": "https://cdn.example.com/alec.jpg",
                "score": 5,
                "source_url": "https://example.com/alec",
                "alt_text": "Alec Whitters",
            }
        ],
        k=1,
    )

    assert selected == [
        {
            "url": "https://cdn.example.com/alec.jpg",
            "source_url": "https://example.com/alec",
            "alt_text": "Alec Whitters",
        }
    ]


def test_lowercase_runtime_config_enables_image_provider(monkeypatch):
    class FakeProvider:
        def __init__(self, model_name=None):
            self.model_name = model_name

        def is_available(self):
            return True

    monkeypatch.setattr(image_skill, "ImageGeneratorProvider", FakeProvider)
    researcher = SimpleNamespace(
        cfg=SimpleNamespace(
            image_generation_enabled=True,
            image_generation_provider="google",
            image_generation_model="models/example",
            image_generation_max_images=2,
        )
    )

    generator = image_skill.ImageGenerator(researcher)

    assert generator.is_enabled() is True
    assert generator.image_provider.model_name == "models/example"


def test_image_quality_filter_rejects_logos_icons_and_svg_artifacts():
    assert not is_likely_content_image(
        "https://example.com/assets/company-logo-scroll.svg", "Company logo"
    )
    assert not is_likely_content_image(
        "https://example.com/favicon.png", "Site icon"
    )
    assert is_likely_content_image(
        "https://example.com/uploads/Alec-Whitters.jpg", "Alec Whitters, CEO"
    )


def test_image_quality_filter_rejects_known_tracking_collectors_and_tiny_pixels():
    assert not is_likely_content_image(
        "https://mc.yandex.com/watch/28584306", ""
    )
    assert not is_likely_content_image(
        "https://cdn.example.com/asset.png?width=1&height=1", ""
    )


def test_image_enrichment_replaces_junk_with_source_page_hero(monkeypatch):
    async def fake_scrape_urls(
        urls,
        _cfg,
        _worker_pool,
        *,
        enforce_public_network=False,
        source_policy=None,
        failure_callback=None,
    ):
        assert enforce_public_network is False
        assert source_policy is None
        assert failure_callback is None
        assert urls == ["https://example.com/alec", "https://example.com/about"]
        return [], [
            {
                "url": "https://example.com/assets/company-logo.svg",
                "score": 5,
                "source_url": "https://example.com/about",
                "alt_text": "Company logo",
            },
            {
                "url": "https://example.com/uploads/alec-whitters.jpg",
                "score": 5,
                "source_url": "https://example.com/alec",
                "alt_text": "Alec Whitters",
            },
        ]

    monkeypatch.setattr(browser_skill, "scrape_urls", fake_scrape_urls)

    class FakeResearcher:
        cfg = SimpleNamespace(max_scraper_workers=1, scraper_rate_limit_delay=0)
        research_sources = [
            {"url": "https://example.com/alec"},
            {"url": "https://example.com/about"},
            {"url": "https://example.com/alec"},
        ]
        research_images = [
            {
                "url": "https://example.com/assets/old-logo.svg",
                "source_url": "https://example.com/alec",
                "alt_text": "Old logo",
            }
        ]

        def get_research_images(self):
            return self.research_images

        def add_research_images(self, images):
            self.research_images.extend(images)

    researcher = FakeResearcher()
    manager = BrowserManager(researcher)

    asyncio.run(manager.enrich_research_images(max_pages=6, k=4))

    assert researcher.research_images == [
        {
            "url": "https://example.com/uploads/alec-whitters.jpg",
            "source_url": "https://example.com/alec",
            "alt_text": "Alec Whitters",
        }
    ]
