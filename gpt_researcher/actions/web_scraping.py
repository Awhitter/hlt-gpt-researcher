from typing import Any
from colorama import Fore, Style

from gpt_researcher.utils.workers import WorkerPool
from gpt_researcher.source_policy import SourcePolicy, SourcePolicyError
from ..scraper import Scraper
from ..config.config import Config
from ..utils.logger import get_formatted_logger

logger = get_formatted_logger()


async def scrape_urls(
    urls,
    cfg: Config,
    worker_pool: WorkerPool,
    *,
    enforce_public_network: bool = False,
    source_policy: SourcePolicy | None = None,
    failure_callback: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Scrapes the urls
    Args:
        urls: List of urls
        cfg: Config (optional)

    Returns:
        tuple[list[dict[str, Any]], list[dict[str, Any]]]: tuple containing scraped content and images

    """
    scraped_data = []
    images = []
    user_agent = (
        cfg.user_agent
        if cfg
        else "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )

    try:
        scraper = Scraper(
            urls,
            user_agent,
            cfg.scraper,
            worker_pool=worker_pool,
            enforce_public_network=enforce_public_network,
            source_policy=source_policy,
            failure_callback=failure_callback,
        )
        scraped_data = await scraper.run()
        for item in scraped_data:
            if 'image_urls' in item:
                images.extend(item['image_urls'])
    except Exception as e:
        print(f"{Fore.RED}Error in scrape_urls: {e}{Style.RESET_ALL}")
        if failure_callback:
            reason = str(e) if isinstance(e, SourcePolicyError) else f"scrape_error:{type(e).__name__}"
            for url in urls:
                failure_callback({"url": url, "reason": reason})

    return scraped_data, images


async def extract_main_content(html_content: str) -> str:
    """
    Extract the main content from HTML.

    Args:
        html_content (str): Raw HTML content.

    Returns:
        str: Extracted main content.
    """
    # Implement content extraction logic here
    # This could involve using libraries like BeautifulSoup or custom parsing logic
    # For now, we'll just return the raw HTML as a placeholder
    return html_content

async def process_scraped_data(scraped_data: list[dict[str, Any]], config: Config) -> list[dict[str, Any]]:
    """
    Process the scraped data to extract and clean the main content.

    Args:
        scraped_data (list[dict[str, Any]]): List of dictionaries containing scraped data.
        config (Config): Configuration object.

    Returns:
        list[dict[str, Any]]: Processed scraped data.
    """
    processed_data = []
    for item in scraped_data:
        if item['status'] == 'success':
            main_content = await extract_main_content(item['content'])
            processed_data.append({
                'url': item['url'],
                'content': main_content,
                'status': 'success'
            })
        else:
            processed_data.append(item)
    return processed_data
