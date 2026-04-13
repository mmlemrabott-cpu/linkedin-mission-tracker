"""
scraper/bereach_scraper.py — BeReach API LinkedIn post scraper.

Runs all keyword queries from config in parallel via ThreadPoolExecutor, paginates each
while hasMore is True (up to max_posts_per_country), normalizes results into
RawPost dicts, applies a 24h safety filter, deduplicates by URL and text hash,
saves raw JSON to disk, and returns the merged final list.

Keywords are sent to BeReach as-is (no country suffix). Country filtering is
handled downstream by Claude via the is_target_location field.
"""

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import requests

from config.config import AppConfig
from scraper.linkedin_scraper import (
    RawPost,
    _extract_contact_info,
    _is_within_24h,
    _save_raw_posts,
    _text_hash,
)

# BeReach API base URL and endpoint
_BASE_URL = "https://api.berea.ch"
_ENDPOINT = "/search/linkedin/posts"

# Results per page (BeReach max is 50)
_PAGE_SIZE = 50

# HTTP timeout in seconds
_REQUEST_TIMEOUT = 30

# Maximum retry attempts on HTTP 429 before giving up on a single page request
_MAX_429_RETRIES = 3

# Base backoff delay (seconds) on first 429 retry — doubles each attempt: 10s → 20s → 40s
_BACKOFF_BASE = 10.0


def scrape_bereach(
    config: AppConfig,
    logger: logging.Logger,
    seen_urls: Optional[Set[str]] = None,
    seen_hashes: Optional[Set[str]] = None,
    keyword_override: Optional[List[str]] = None,
) -> List[RawPost]:
    """
    Fetch LinkedIn posts from the BeReach API for all keywords in config.

    All queries run concurrently. Each paginates while hasMore is True or until
    max_posts_per_country is reached. Results are merged and deduplicated by URL
    and text hash (within-run and cross-run). Saves raw results to
    data/raw_posts_{YYYY-MM-DD}.json.

    Keywords are sent to BeReach exactly as written in config — no country suffix
    is appended. Country filtering is delegated to Claude (is_target_location field).

    Args:
        config: Application configuration (provides bereach_api_token, search_keywords,
                max_posts_per_country).
        logger: Logger instance.
        seen_urls: Optional set of post URLs already written in previous runs.
        seen_hashes: Optional set of text hashes already written in previous runs.
        keyword_override: If provided, use these keywords instead of config.search_keywords.
                          Used by the remote jobs pipeline (RUN_MODE=job).

    Returns:
        List of deduplicated RawPost dicts, all published within the last 24 hours.
    """
    seen_urls_global: Set[str] = seen_urls if seen_urls is not None else set()
    seen_hashes_global: Set[str] = seen_hashes if seen_hashes is not None else set()

    # Use keyword_override if provided (remote jobs pipeline), else fall back to config
    keyword_queries: List[str] = list(keyword_override) if keyword_override else list(config.search_keywords)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    headers = {
        "Authorization": f"Bearer {config.bereach_api_token}",
        "Content-Type": "application/json",
    }

    logger.info(
        "[bereach] Running %d keyword queries in parallel.", len(keyword_queries)
    )

    # Fetch all pages for each query in parallel, staggered by 10s to avoid 429.
    # 10s gap ensures earlier workers' pagination requests don't collide with
    # later workers' first requests under BeReach's per-minute rate limit.
    with ThreadPoolExecutor(max_workers=len(keyword_queries)) as executor:
        futures = {
            executor.submit(
                _fetch_all_pages, keywords, headers, config.max_posts_per_country, logger,
                initial_delay=i * 10.0,
            ): keywords
            for i, keywords in enumerate(keyword_queries)
        }
        raw_batches: List[List[Dict[str, Any]]] = []
        for future in as_completed(futures):
            keywords = futures[future]
            try:
                items = future.result()
                raw_batches.append(items)
            except Exception as exc:
                logger.error("[bereach] Query failed — keywords='%s': %s", keywords, exc)
                raw_batches.append([])

    # Merge and deduplicate across both batches
    seen_urls_run: set = set()
    seen_text_hashes_run: set = set()
    all_posts: List[RawPost] = []

    for raw_items in raw_batches:
        for item in raw_items:
            post = _normalize_bereach_post(item)
            if post is None:
                continue
            if not _is_within_24h(post["post_date"], logger):
                continue
            if post["post_url"] in seen_urls_run:
                logger.debug("[bereach] duplicate URL skipped: %s", post["post_url"])
                continue
            text_hash = _text_hash(post["post_text"])
            if text_hash in seen_text_hashes_run:
                logger.debug("[bereach] near-duplicate text skipped: %s", post["post_url"])
                continue
            if post["post_url"] in seen_urls_global:
                logger.debug("[bereach] cross-run duplicate URL skipped: %s", post["post_url"])
                continue
            if text_hash in seen_hashes_global:
                logger.debug("[bereach] cross-run repost skipped: %s", post["post_url"])
                continue

            seen_urls_run.add(post["post_url"])
            seen_text_hashes_run.add(text_hash)
            all_posts.append(post)

    logger.info("[bereach] Total unique posts within 24h: %d", len(all_posts))
    _save_raw_posts(all_posts, date_str, logger)
    return all_posts


def _fetch_all_pages(
    keywords: str,
    headers: Dict[str, str],
    max_posts: int,
    logger: logging.Logger,
    initial_delay: float = 0.0,
) -> List[Dict[str, Any]]:
    """
    Fetch all paginated results for a single keyword query from the BeReach API.

    Paginates while hasMore is True and the collected item count is below max_posts.
    Adds a random delay between pages to respect rate limits. Each individual page
    request is wrapped in _post_with_retry which handles HTTP 429 with exponential
    backoff (10s → 20s → 40s, up to _MAX_429_RETRIES retries).

    Args:
        keywords: Boolean keyword query string.
        headers: HTTP headers including Authorization.
        max_posts: Maximum number of raw items to collect.
        logger: Logger instance.
        initial_delay: Seconds to wait before the first request (used to stagger
                       parallel calls and avoid simultaneous 429 errors).

    Returns:
        List of raw item dicts from the API response.
    """
    if initial_delay > 0:
        time.sleep(initial_delay)

    collected: List[Dict[str, Any]] = []
    start = 0
    page = 0

    while len(collected) < max_posts:
        if page > 0:
            time.sleep(random.uniform(0.5, 1.5))

        payload: Dict[str, Any] = {
            "keywords": keywords,
            "sortBy": "relevance",
            "datePosted": "past-24h",
            "count": _PAGE_SIZE,
            "start": start,
        }

        data = _post_with_retry(keywords, payload, headers, logger)
        if data is None:
            break

        items = data.get("items", [])
        has_more = data.get("hasMore", False)
        credits_used = data.get("creditsUsed", 0)

        logger.info(
            "[bereach] keywords='%.60s...' start=%d → %d items (hasMore=%s, credits=%s)",
            keywords, start, len(items), has_more, credits_used,
        )

        collected.extend(items)

        if not has_more or not items:
            break

        start += _PAGE_SIZE
        page += 1

    return collected


def _post_with_retry(
    keywords: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    logger: logging.Logger,
) -> Optional[Dict[str, Any]]:
    """
    POST to the BeReach API with exponential-backoff retry on HTTP 429.

    Retries up to _MAX_429_RETRIES times on 429 responses. Backoff doubles each
    attempt: 10s → 20s → 40s. Non-429 HTTP errors and network errors abort
    immediately without retry.

    Args:
        keywords: Keyword query string (used only for log messages).
        payload: JSON request body.
        headers: HTTP headers including Authorization.
        logger: Logger instance.

    Returns:
        Parsed JSON response dict, or None if all attempts failed.
    """
    start = payload.get("start", 0)

    for attempt in range(1, _MAX_429_RETRIES + 2):  # 1 initial attempt + up to 3 retries
        try:
            logger.debug(
                "[bereach] POST %s%s keywords='%.60s...' start=%d (attempt %d)",
                _BASE_URL, _ENDPOINT, keywords, start, attempt,
            )
            resp = requests.post(
                f"{_BASE_URL}{_ENDPOINT}",
                json=payload,
                headers=headers,
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()

        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 429 and attempt <= _MAX_429_RETRIES:
                backoff = _BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "[bereach] HTTP 429 (keywords='%.60s...', start=%d) — "
                    "retry %d/%d in %.0fs",
                    keywords, start, attempt, _MAX_429_RETRIES, backoff,
                )
                time.sleep(backoff)
                continue
            logger.error(
                "[bereach] HTTP %d error (keywords='%.60s...', start=%d): %s",
                status, keywords, start, exc,
            )
            return None

        except Exception as exc:
            logger.error(
                "[bereach] Request failed (keywords='%.60s...', start=%d): %s",
                keywords, start, exc,
            )
            return None

    logger.error(
        "[bereach] Gave up after %d retries on 429 (keywords='%.60s...', start=%d)",
        _MAX_429_RETRIES, keywords, start,
    )
    return None


def _normalize_bereach_post(item: Dict[str, Any]) -> Optional[RawPost]:
    """
    Map a BeReach API response item to the canonical RawPost structure.

    Returns None if essential fields (postUrl, text) are missing.
    Converts the date field from milliseconds epoch to an ISO 8601 UTC string.
    Extracts contact_info via regex from post_text.

    Args:
        item: Single item from the BeReach /search/linkedin/posts response.

    Returns:
        Normalized RawPost or None if the item is malformed.
    """
    post_url = item.get("postUrl", "")
    post_text = item.get("text", "")

    if not post_url or not isinstance(post_text, str) or not post_text:
        return None

    # BeReach returns date as milliseconds since epoch (integer)
    raw_date = item.get("date")
    if raw_date and isinstance(raw_date, (int, float)):
        try:
            post_date = datetime.fromtimestamp(raw_date / 1000, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            post_date = datetime.now(timezone.utc).isoformat()
    else:
        post_date = datetime.now(timezone.utc).isoformat()

    author = item.get("author") or {}
    author_name = author.get("name", "")
    author_title = author.get("headline", "")
    author_profile_url = author.get("profileUrl", "")

    return RawPost(
        post_url=post_url,
        author_name=author_name,
        author_title=author_title,
        author_profile_url=author_profile_url,
        post_text=post_text,
        post_date=post_date,
        likes_count=int(item.get("likesCount") or 0),
        comments_count=int(item.get("commentsCount") or 0),
        contact_info=_extract_contact_info(post_text),
        country="",
        keyword=item.get("_keyword", ""),
    )
