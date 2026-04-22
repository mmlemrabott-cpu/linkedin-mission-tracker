"""
scraper/apify_scraper.py — Apify LinkedIn post scraper.

Replaces BeReach as the scraping backend. Uses the Apify actor
`supreme_coder/linkedin-post` (actor ID: Wpp1BZ6yGWjySadk3) which accepts
LinkedIn search URLs and returns structured post data.

Uses the Apify REST API directly via `requests` — no apify-client library
dependency, avoiding transitive version-conflict issues.

Endpoint used:
  POST /v2/acts/{actorId}/runs/sync-get-dataset-items?token={token}&timeout=300
  This synchronous endpoint runs the actor and streams dataset items back once
  the run completes (or times out after `timeout` seconds).

Design:
  - All keyword strings are converted to LinkedIn search URLs with
    datePosted=past-24h to limit results to the last 24 hours.
  - A single actor run is triggered with all keyword URLs batched together.
    Apify handles LinkedIn rate-limiting internally — no client-side batching
    or retry logic is required.
  - The actor returns structured post objects. Each is normalised into the
    canonical RawPost dict and filtered to within-24h posts.
  - Deduplication (URL + text hash) is applied both within-run and
    cross-run (via the seen_urls / seen_hashes sets from Dedup_Index).
  - Raw results are saved to data/raw_posts_{YYYY-MM-DD}.json for debugging.

Field mapping (Apify → RawPost):
  url               → post_url
  text              → post_text
  postedAtISO       → post_date  (ISO 8601 UTC — no conversion needed)
  authorName        → author_name
  authorHeadline    → author_title
  authorProfileUrl  → author_profile_url
  numLikes          → likes_count
  numComments       → comments_count
  contact_info      ← extracted from post_text via regex
  country           ← "" (country filtering delegated to Claude scorer)
  keyword           ← "" (Apify does not report which URL produced each post)
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
from urllib.parse import quote

import requests

from config.config import AppConfig
from scraper.linkedin_scraper import (
    RawPost,
    _extract_contact_info,
    _is_within_24h,
    _save_raw_posts,
    _text_hash,
)

# Apify actor ID for supreme_coder/linkedin-post
_ACTOR_ID = "Wpp1BZ6yGWjySadk3"

# Apify REST API base URL
_APIFY_BASE_URL = "https://api.apify.com/v2"

# Synchronous run endpoint — blocks until the actor finishes and returns
# dataset items directly. `timeout` is the actor's max runtime in seconds.
_ACTOR_SYNC_TIMEOUT_SECONDS = 300

# HTTP request timeout for the synchronous actor call (actor timeout + margin)
_HTTP_REQUEST_TIMEOUT = _ACTOR_SYNC_TIMEOUT_SECONDS + 30

# LinkedIn search URL template — keywords are URL-encoded, datePosted restricts
# to the last 24 hours so that results align with our daily run cadence.
_LINKEDIN_SEARCH_URL_TEMPLATE = (
    "https://www.linkedin.com/search/results/content/"
    "?keywords={encoded_keywords}&datePosted=%5B%22past-24h%22%5D"
)


def _keyword_to_linkedin_url(keyword: str) -> str:
    """
    Convert a boolean keyword string to a LinkedIn content search URL.

    The keyword is URL-encoded and embedded into a LinkedIn search URL
    filtered to the last 24 hours.

    Args:
        keyword: LinkedIn boolean keyword string (e.g. '"mission" AND "freelance"').

    Returns:
        Full LinkedIn search URL with encoded keyword and datePosted filter.
    """
    encoded = quote(keyword, safe="")
    return _LINKEDIN_SEARCH_URL_TEMPLATE.format(encoded_keywords=encoded)


def scrape_apify(
    config: AppConfig,
    logger: logging.Logger,
    seen_urls: Optional[Set[str]] = None,
    seen_hashes: Optional[Set[str]] = None,
    keyword_override: Optional[List[str]] = None,
) -> List[RawPost]:
    """
    Fetch LinkedIn posts from the Apify `supreme_coder/linkedin-post` actor.

    Converts each keyword string to a LinkedIn search URL (past-24h filter)
    and triggers a single synchronous Apify actor run with all URLs batched.
    Apify handles LinkedIn rate-limiting internally.

    Uses the Apify REST API directly (no apify-client library) to avoid
    transitive dependency version conflicts.

    Results are normalised into RawPost dicts, filtered to within-24h posts,
    deduplicated by URL and text hash (within-run and cross-run), and saved to
    data/raw_posts_{YYYY-MM-DD}.json.

    Args:
        config: Application configuration (provides apify_api_token,
                search_keywords, max_posts_per_country).
        logger: Logger instance.
        seen_urls: Optional set of post URLs already written in previous runs.
        seen_hashes: Optional set of text hashes already written in previous
                     runs.
        keyword_override: If provided, use these keywords instead of
                          config.search_keywords. Used by the remote jobs
                          pipeline (RUN_MODE=job).

    Returns:
        List of deduplicated RawPost dicts, all published within the last
        24 hours.
    """
    seen_urls_global: Set[str] = seen_urls if seen_urls is not None else set()
    seen_hashes_global: Set[str] = seen_hashes if seen_hashes is not None else set()

    keyword_queries: List[str] = (
        list(keyword_override) if keyword_override else list(config.search_keywords)
    )

    if not keyword_queries:
        logger.warning("[apify] No keyword queries configured — returning empty list.")
        return []

    # Convert keyword strings to LinkedIn search URLs
    linkedin_urls = [_keyword_to_linkedin_url(kw) for kw in keyword_queries]

    logger.info(
        "[apify] Starting actor run with %d keyword URL(s) — actor=%s, limitPerSource=%d.",
        len(linkedin_urls),
        _ACTOR_ID,
        config.max_posts_per_country,
    )
    for i, (kw, url) in enumerate(zip(keyword_queries, linkedin_urls)):
        logger.debug("[apify] Keyword %d: '%s' → %s", i + 1, kw[:80], url)

    # Call the synchronous Apify run endpoint
    run_input: Dict[str, Any] = {
        "urls": linkedin_urls,
        "deepScrape": False,          # faster; sufficient for post text + metadata
        "limitPerSource": config.max_posts_per_country,
        "rawData": False,
    }

    endpoint = (
        f"{_APIFY_BASE_URL}/acts/{_ACTOR_ID}/runs/sync-get-dataset-items"
        f"?token={config.apify_api_token}&timeout={_ACTOR_SYNC_TIMEOUT_SECONDS}"
    )

    try:
        logger.info(
            "[apify] POST sync-get-dataset-items (HTTP timeout=%ds, actor timeout=%ds).",
            _HTTP_REQUEST_TIMEOUT,
            _ACTOR_SYNC_TIMEOUT_SECONDS,
        )
        resp = requests.post(
            endpoint,
            json=run_input,
            timeout=_HTTP_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        raw_items: List[Dict[str, Any]] = resp.json()
    except requests.exceptions.HTTPError as exc:
        logger.error(
            "[apify] HTTP %d error from Apify API: %s",
            exc.response.status_code if exc.response is not None else 0,
            exc,
        )
        return []
    except Exception as exc:
        logger.error("[apify] Actor run failed: %s", exc)
        return []

    if not isinstance(raw_items, list):
        logger.error(
            "[apify] Unexpected response format (expected list, got %s): %s",
            type(raw_items).__name__,
            str(raw_items)[:200],
        )
        return []

    logger.info("[apify] Retrieved %d raw item(s) from actor run.", len(raw_items))

    # Normalise, filter, and deduplicate
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    seen_urls_run: Set[str] = set()
    seen_text_hashes_run: Set[str] = set()
    all_posts: List[RawPost] = []

    for item in raw_items:
        post = _normalize_apify_post(item)
        if post is None:
            continue
        if not _is_within_24h(post["post_date"], logger):
            continue
        if post["post_url"] in seen_urls_run:
            logger.debug("[apify] duplicate URL skipped: %s", post["post_url"])
            continue
        text_hash = _text_hash(post["post_text"])
        if text_hash in seen_text_hashes_run:
            logger.debug("[apify] near-duplicate text skipped: %s", post["post_url"])
            continue
        if post["post_url"] in seen_urls_global:
            logger.debug("[apify] cross-run duplicate URL skipped: %s", post["post_url"])
            continue
        if text_hash in seen_hashes_global:
            logger.debug("[apify] cross-run repost skipped: %s", post["post_url"])
            continue

        seen_urls_run.add(post["post_url"])
        seen_text_hashes_run.add(text_hash)
        all_posts.append(post)

    logger.info("[apify] Total unique posts within 24h: %d", len(all_posts))
    _save_raw_posts(all_posts, date_str, logger)
    return all_posts


def _normalize_apify_post(item: Dict[str, Any]) -> Optional[RawPost]:
    """
    Map an Apify `supreme_coder/linkedin-post` response item to the canonical
    RawPost structure.

    Returns None if essential fields (url, text) are missing or empty.
    The `postedAtISO` field is already an ISO 8601 UTC string — no conversion
    from milliseconds is needed.

    Args:
        item: Single item from the Apify actor dataset.

    Returns:
        Normalized RawPost or None if the item is malformed.
    """
    post_url = item.get("url", "")
    post_text = item.get("text", "")

    if not post_url or not isinstance(post_text, str) or not post_text:
        return None

    # postedAtISO is already ISO 8601 UTC (e.g. "2025-04-21T09:34:00.000Z")
    post_date = item.get("postedAtISO", "")
    if not post_date:
        # Fallback: convert postedAtTimestamp (ms epoch) if ISO field is absent
        raw_ts = item.get("postedAtTimestamp")
        if raw_ts and isinstance(raw_ts, (int, float)):
            try:
                post_date = datetime.fromtimestamp(
                    raw_ts / 1000, tz=timezone.utc
                ).isoformat()
            except (OSError, OverflowError, ValueError):
                post_date = datetime.now(timezone.utc).isoformat()
        else:
            post_date = datetime.now(timezone.utc).isoformat()

    return RawPost(
        post_url=post_url,
        author_name=item.get("authorName", ""),
        author_title=item.get("authorHeadline", ""),
        author_profile_url=item.get("authorProfileUrl", ""),
        post_text=post_text,
        post_date=post_date,
        likes_count=int(item.get("numLikes") or 0),
        comments_count=int(item.get("numComments") or 0),
        contact_info=_extract_contact_info(post_text),
        country="",    # Country filtering delegated to Claude scorer
        keyword="",    # Apify does not report which search URL produced each post
    )
