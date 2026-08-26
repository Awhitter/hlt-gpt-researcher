from bs4 import BeautifulSoup
import os
from ..utils import get_relevant_images
from gpt_researcher.source_policy import (
    SourcePolicyError,
    canonicalize_url,
    require_policy_source_url,
)

class FireCrawl:

    def __init__(self, link, session=None):
        self.link = link
        self.session = session
        from firecrawl import FirecrawlApp
        self.firecrawl = FirecrawlApp(api_key=self.get_api_key(), api_url=self.get_server_url())

    def get_api_key(self) -> str:
        """
        Gets the FireCrawl API key
        Returns:
        Api key (str)
        """
        try:
            api_key = os.environ["FIRECRAWL_API_KEY"]
        except KeyError:
            raise Exception(
                "FireCrawl API key not found. Please set the FIRECRAWL_API_KEY environment variable.")
        return api_key

    def get_server_url(self) -> str:
        """
        Gets the FireCrawl server URL.
        Default to official FireCrawl server ('https://api.firecrawl.dev').
        Returns:
        server url (str)
        """
        try:
            server_url = os.environ["FIRECRAWL_SERVER_URL"]
        except KeyError:
            server_url = 'https://api.firecrawl.dev'
        return server_url

    def scrape(self) -> tuple:
        """
        This function extracts content and title from a specified link using the FireCrawl Python SDK,
        images from the link are extracted using the functions from `gpt_researcher/scraper/utils.py`.

        Returns:
          The `scrape` method returns a tuple containing the extracted content, a list of image URLs, and
        the title of the webpage specified by the `self.link` attribute. It uses the FireCrawl Python SDK to
        extract and clean content from the webpage. If any exception occurs during the process, an error
        message is printed and an empty result is returned.
        """

        try:
            strict_network = getattr(
                self.session, "_gptr_enforce_public_network", False
            )
            scrape_options = {"formats": ["markdown"]}
            if strict_network:
                # Strict evidence must not inherit stale or cross-key provider
                # cache state. Firecrawl remains the remote fetch boundary.
                scrape_options.update(max_age=0, store_in_cache=False)
            # Fixed: Changed from scrape_url() to scrape() to match FireCrawl SDK v4.6.0+
            response = self.firecrawl.scrape(url=self.link, **scrape_options)

            # Check if the page has been scraped successfully
            # Fixed: Access metadata attributes directly (not as dict keys)
            if response.metadata and response.metadata.error:
                print("Scrape failed! : " + str(response.metadata.error))
                return "", [], ""
            elif response.metadata and response.metadata.status_code and response.metadata.status_code != 200:
                print(f"Scrape failed! Status code: {response.metadata.status_code}")
                return "", [], ""

            if strict_network:
                requested_url = getattr(response.metadata, "source_url", None)
                resolved_url = getattr(response.metadata, "url", None)
                if not requested_url or not resolved_url:
                    raise SourcePolicyError(
                        "strict Firecrawl response did not preserve requested and "
                        "resolved URL provenance"
                    )
                canonical_link = canonicalize_url(self.link)
                canonical_requested = canonicalize_url(str(requested_url))
                canonical_resolved = canonicalize_url(str(resolved_url))
                for attested_url in (requested_url, resolved_url):
                    require_policy_source_url(
                        self.session._gptr_source_policy,
                        str(attested_url),
                        resolve_dns=True,
                    )
                if canonical_requested != canonical_link:
                    raise SourcePolicyError(
                        "strict Firecrawl requested URL attestation did not match "
                        "the requested source"
                    )
                if canonical_resolved != canonical_link:
                    raise SourcePolicyError(
                        "strict Firecrawl resolved URL did not match the requested "
                        "source; redirected content cannot be relabeled"
                    )

            # Extract the content (markdown) and title from FireCrawl response
            # Fixed: Access attributes directly (not as dict keys)
            content = response.markdown if response.markdown else ""
            title = response.metadata.title if response.metadata and response.metadata.title else ""

            if strict_network:
                # Strict runs never fetch target pages from the MCP service
                # network. Firecrawl owns the remote fetch; generated images
                # remain separately opt-in and source images stay empty.
                image_urls = []
            else:
                try:
                    response_bs = self.session.get(self.link, timeout=4)
                    soup = BeautifulSoup(
                        response_bs.content,
                        "lxml",
                        from_encoding=response_bs.encoding,
                    )
                    image_urls = get_relevant_images(soup, self.link)
                except Exception as image_error:
                    print(f"Image enrichment failed; keeping Firecrawl text: {image_error}")
                    image_urls = []

            return content, image_urls, title

        except SourcePolicyError:
            raise
        except Exception as e:
            print("Error! : " + str(e))
            return "", [], ""
