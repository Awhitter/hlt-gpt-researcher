"""Utility functions for web scraping.

This module provides helper functions for extracting content, images,
and processing HTML from web pages.
"""

import hashlib
import logging
import re
from urllib.parse import parse_qs, urljoin, urlparse

import bs4
from bs4 import BeautifulSoup


def is_likely_content_image(image_url: str, alt_text: str = "") -> bool:
    """Reject common chrome/brand assets before they reach a research report."""

    parsed = urlparse(image_url)
    path = parsed.path.lower()
    description = f"{path} {alt_text.lower()}"
    if parsed.scheme not in {"http", "https"}:
        return False
    if path.endswith((".svg", ".ico")):
        return False
    noise_terms = (
        "favicon",
        "logo",
        "sprite",
        "tracking-pixel",
        "spacer.",
        "placeholder",
        "games-assets",
        "dmca compliant",
        "site icon",
    )
    return not any(term in description for term in noise_terms)


def get_relevant_images(soup: BeautifulSoup, url: str) -> list:
    """Extract relevant images from the page"""
    image_urls = []
    seen_urls = set()

    def add_image(image_url: str, score: int, alt_text: str = "") -> None:
        resolved_url = urljoin(url, image_url)
        if (
            not resolved_url.startswith(("http://", "https://"))
            or resolved_url in seen_urls
            or not is_likely_content_image(resolved_url, alt_text)
        ):
            return
        seen_urls.add(resolved_url)
        image_urls.append(
            {
                "url": resolved_url,
                "score": score,
                "source_url": url,
                "alt_text": alt_text.strip(),
            }
        )
    
    try:
        # Social-card metadata is usually the page's intended hero image and
        # works when the visible markup lazy-loads every <img>.
        og_alt = soup.find("meta", attrs={"property": "og:image:alt"})
        twitter_alt = soup.find("meta", attrs={"name": "twitter:image:alt"})
        for selector in (
            {"property": "og:image"},
            {"property": "og:image:url"},
            {"name": "twitter:image"},
            {"name": "twitter:image:src"},
        ):
            meta = soup.find("meta", attrs=selector)
            if meta and meta.get("content"):
                alt = ""
                if selector.get("property", "").startswith("og:") and og_alt:
                    alt = og_alt.get("content", "")
                elif selector.get("name", "").startswith("twitter:") and twitter_alt:
                    alt = twitter_alt.get("content", "")
                add_image(meta["content"], 5, alt)

        # Find all img tags with src attribute
        all_images = soup.find_all('img', src=True)
        
        for img in all_images:
            img_src = img['src']
            if urljoin(url, img_src).startswith(('http://', 'https://')):
                score = 0
                # Check for relevant classes
                if any(cls in img.get('class', []) for cls in ['header', 'featured', 'hero', 'thumbnail', 'main', 'content']):
                    score = 4  # Higher score
                # Check for size attributes
                elif img.get('width') and img.get('height'):
                    width = parse_dimension(img['width'])
                    height = parse_dimension(img['height'])
                    if width and height:
                        if width >= 2000 and height >= 1000:
                            score = 3  # Medium score (very large images)
                        elif width >= 1600 or height >= 800:
                            score = 2  # Lower score
                        elif width >= 800 or height >= 500:
                            score = 1  # Lowest score
                        elif width >= 500 or height >= 300:
                            score = 0  # Lowest score
                        else:
                            continue  # Skip small images
                
                add_image(img_src, score, img.get("alt", ""))
        
        # Sort images by score (highest first)
        sorted_images = sorted(image_urls, key=lambda x: x['score'], reverse=True)
        
        return sorted_images[:10]  # Ensure we don't return more than 10 images in total
    
    except Exception as e:
        logging.error(f"Error in get_relevant_images: {e}")
        return []

def parse_dimension(value: str) -> int:
    """Parse dimension value, handling px units"""
    if value.lower().endswith('px'):
        value = value[:-2]  # Remove 'px' suffix
    try:
        # Convert to float first to handle decimal values like '409.12'
        return int(float(value))
    except (ValueError, TypeError) as e:
        print(f"Error parsing dimension value {value}: {e}")
        return None

def extract_title(soup: BeautifulSoup) -> str:
    """Extract the title from the BeautifulSoup object"""
    return soup.title.string if soup.title else ""

def get_image_hash(image_url: str) -> str:
    """Calculate a simple hash based on the image filename and essential query parameters"""
    try:
        parsed_url = urlparse(image_url)
        
        # Extract the filename
        filename = parsed_url.path.split('/')[-1]
        
        # Extract essential query parameters (e.g., 'url' for CDN-served images)
        query_params = parse_qs(parsed_url.query)
        essential_params = query_params.get('url', [])
        
        # Combine filename and essential parameters
        image_identifier = filename + ''.join(essential_params)
        
        # Calculate hash
        return hashlib.md5(image_identifier.encode()).hexdigest()
    except Exception as e:
        logging.error(f"Error calculating image hash for {image_url}: {e}")
        return None


def clean_soup(soup: BeautifulSoup) -> BeautifulSoup:
    """Clean the soup by removing unwanted tags"""
    for tag in soup.find_all(
        [
            "script",
            "style",
            "footer",
            "header",
            "nav",
            "menu",
            "sidebar",
            "svg",
        ]
    ):
        tag.decompose()

    disallowed_class_set = {"nav", "menu", "sidebar", "footer"}

    # clean tags with certain classes
    def does_tag_have_disallowed_class(elem) -> bool:
        if not isinstance(elem, bs4.Tag):
            return False

        return any(
            cls_name in disallowed_class_set for cls_name in elem.get("class", [])
        )

    for tag in soup.find_all(does_tag_have_disallowed_class):
        tag.decompose()

    return soup


def get_text_from_soup(soup: BeautifulSoup) -> str:
    """Get the relevant text from the soup with improved filtering"""
    text = soup.get_text(strip=True, separator="\n")
    # Remove excess whitespace
    text = re.sub(r"\s{2,}", " ", text)
    return text
