"""
scraper/apify_scraper.py — Apify LinkedIn post scraper.

Uses the Apify actor `harvestapi/linkedin-post-search` which accepts plain
keyword strings and returns structured LinkedIn post data. No LinkedIn cookies
or account required.

Uses the Apify REST API directly via `requests` — no apify-client library
dependency, avoiding transitive version-conflict issues.

Endpoint used:
  POST /v2/acts/{actorId}/run-sync-get-dataset-items?token={token}&timeout=300
  This synchronous endpoint runs the actor and streams dataset items back once
  the run completes (or times out after `timeout` seconds).

Design:
  - Keyword strings are passed directly as `searchQueries` — no URL construction.
  - `postedLimit: "24h"` is set in the actor input to restrict to the last 24h.
  - A single actor run processes all keyword queries.
  - The actor returns structured post objects. Each is normalised into the
    canonical RawPost dict and filtered to within-24h posts.
  - Deduplication (URL + text hash) is applied both within-run and
    cross-run (via the seen_urls / seen_hashes sets from Dedup_Index).
  - Raw results are saved to data/raw_posts_{YYYY-MM-DD}.json for debugging.
  - After the sync call, the most recent completed run is fetched to capture
    the real Apify cost (usageTotalUsd).

Field mapping (harvestapi/linkedin-post-search → RawPost):
  linkedinUrl           → post_url
  content               → post_text
  postedAt.date         → post_date  (ISO 8601 UTC, e.g. "2026-06-10T18:08:52.091Z")
  author.name           → author_name
  author.info           → author_title  (headline/job title)
  author.linkedinUrl    → author_profile_url
  engagement.likes      → likes_count
  engagement.comments   → comments_count
  contact_info          ← extracted from post_text via regex
  country               ← "" (country filtering delegated to Claude scorer)
  keyword               ← "" (actor does not report which query produced each post)
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from config.config import AppConfig
from scraper.linkedin_scraper import (
    RawPost,
    _extract_contact_info,
    _is_within_24h,
    _save_raw_posts,
    _text_hash,
)

# Apify actor slug — used in REST API URLs (namespace~name format)
_ACTOR_ID = "harvestapi~linkedin-post-search"

# Apify REST API base URL
_APIFY_BASE_URL = "https://api.apify.com/v2"

# Synchronous run endpoint — blocks until the actor finishes and returns
# dataset items directly. `timeout` is the actor's max runtime in seconds.
_ACTOR_SYNC_TIMEOUT_SECONDS = 300

# HTTP request timeout for the synchronous actor call (actor timeout + margin)
_HTTP_REQUEST_TIMEOUT = _ACTOR_SYNC_TIMEOUT_SECONDS + 30


class ApifyRunStats(dict):
    """
    Stats dict returned alongside the posts list by scrape_apify().

    Keys:
        posts_raw: int          — items returned by Apify before any filtering
        posts_unique: int       — after 24h filter + deduplication
        keywords_count: int     — number of keyword queries sent to the actor
        cost_usd: float         — real cost from Apify billing API (-1 if unavailable)
        duration_seconds: float — wall-clock time for the Apify actor call
        status: str             — "success" or "error"
    """


def _fetch_last_run_cost(token: str, logger: logging.Logger) -> float:
    """
    Fetch the USD cost of the most recently completed Apify actor run.

    Queries GET /v2/acts/{actorId}/runs?limit=1&status=SUCCEEDED to retrieve
    the last successful run and extract usageTotalUsd.

    Args:
        token: Apify API token.
        logger: Logger instance.

    Returns:
        Cost in USD as a float, or -1.0 if the value cannot be retrieved.
    """
    try:
        resp = requests.get(
            f"{_APIFY_BASE_URL}/acts/{_ACTOR_ID}/runs",
            params={"token": token, "limit": 1, "status": "SUCCEEDED"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {}).get("items", [])
        if data:
            cost = data[0].get("usageTotalUsd")
            if cost is not None:
                return float(cost)
    except Exception as exc:
        logger.debug("[apify] Could not fetch run cost from billing API: %s", exc)
    return -1.0


def scrape_apify(
    config: AppConfig,
    logger: logging.Logger,
    seen_urls: Optional[Set[str]] = None,
    seen_hashes: Optional[Set[str]] = None,
    keyword_override: Optional[List[str]] = None,
) -> Tuple[List[RawPost], ApifyRunStats]:
    """
    Fetch LinkedIn posts from the Apify `harvestapi/linkedin-post-search` actor.

    Passes keyword strings directly as `searchQueries` and sets `postedLimit`
    to "24h" so the actor only returns posts from the last 24 hours.
    A single synchronous Apify actor run processes all keyword queries.

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
        Tuple of (deduplicated RawPost list, ApifyRunStats dict).
        All posts are published within the last 24 hours.
    """
    seen_urls_global: Set[str] = seen_urls if seen_urls is not None else set()
    seen_hashes_global: Set[str] = seen_hashes if seen_hashes is not None else set()

    keyword_queries: List[str] = (
        list(keyword_override) if keyword_override else list(config.search_keywords)
    )

    _error_stats = ApifyRunStats(
        posts_raw=0, posts_unique=0, keywords_count=len(keyword_queries),
        cost_usd=-1.0, duration_seconds=0.0, status="error",
    )

    if not keyword_queries:
        logger.warning("[apify] No keyword queries configured — returning empty list.")
        return [], _error_stats

    logger.info(
        "[apify] Starting actor run with %d keyword(s) — actor=%s, maxPosts=%d.",
        len(keyword_queries),
        _ACTOR_ID,
        config.max_posts_per_country,
    )
    for i, kw in enumerate(keyword_queries):
        logger.debug("[apify] Keyword %d: '%s'", i + 1, kw[:80])

    # Pass keyword strings directly — no URL construction needed.
    # postedLimit="24h" restricts results to the last 24 hours server-side.
    run_input: Dict[str, Any] = {
        "searchQueries": keyword_queries,
        "maxPosts": config.max_posts_per_country,
        "postedLimit": "24h",
        "sortBy": "date",
        "scrapeReactions": False,
        "scrapeComments": False,
    }

    endpoint = (
        f"{_APIFY_BASE_URL}/acts/{_ACTOR_ID}/run-sync-get-dataset-items"
        f"?token={config.apify_api_token}&timeout={_ACTOR_SYNC_TIMEOUT_SECONDS}"
    )

    t0 = time.time()
    try:
        logger.info(
            "[apify] POST run-sync-get-dataset-items (HTTP timeout=%ds, actor timeout=%ds).",
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
        return [], _error_stats
    except Exception as exc:
        logger.error("[apify] Actor run failed: %s", exc)
        return [], _error_stats

    duration = time.time() - t0

    if not isinstance(raw_items, list):
        logger.error(
            "[apify] Unexpected response format (expected list, got %s): %s",
            type(raw_items).__name__,
            str(raw_items)[:200],
        )
        return [], _error_stats

    posts_raw = len(raw_items)
    logger.info("[apify] Retrieved %d raw item(s) from actor run (%.1fs).", posts_raw, duration)

    # Fetch real billing cost from Apify runs API
    cost_usd = _fetch_last_run_cost(config.apify_api_token, logger)
    if cost_usd >= 0:
        logger.info("[apify] Apify run cost: $%.4f USD.", cost_usd)
    else:
        logger.debug("[apify] Apify billing cost unavailable — will show N/A in dashboard.")

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

    posts_unique = len(all_posts)
    logger.info("[apify] Total unique posts within 24h: %d", posts_unique)
    _save_raw_posts(all_posts, date_str, logger)

    stats = ApifyRunStats(
        posts_raw=posts_raw,
        posts_unique=posts_unique,
        keywords_count=len(keyword_queries),
        cost_usd=cost_usd,
        duration_seconds=round(duration, 1),
        status="success",
    )
    return all_posts, stats


def _normalize_apify_post(item: Dict[str, Any]) -> Optional[RawPost]:
    """
    Map a `harvestapi/linkedin-post-search` response item to the canonical
    RawPost structure.

    Returns None if essential fields (linkedinUrl, content) are missing or empty.

    harvestapi uses:
      - "linkedinUrl" for the post URL
      - "content" for the post text
      - "postedAt" nested object with "date" (ISO string) and "timestamp" (ms epoch)
      - "author" nested object with "name", "info" (headline), "linkedinUrl"
      - "engagement" nested object with "likes", "comments"

    Args:
        item: Single item from the Apify actor dataset.

    Returns:
        Normalized RawPost or None if the item is malformed.
    """
    post_url = item.get("linkedinUrl", "")
    post_text = item.get("content", "")

    if not post_url or not isinstance(post_text, str) or not post_text:
        return None

    # postedAt is a nested object: {"date": "2026-06-10T18:08:52.091Z", "timestamp": 1781114932091}
    posted_at: Dict[str, Any] = item.get("postedAt") or {}
    post_date = posted_at.get("date", "")
    if not post_date:
        raw_ts = posted_at.get("timestamp")
        if raw_ts and isinstance(raw_ts, (int, float)):
            try:
                post_date = datetime.fromtimestamp(
                    raw_ts / 1000, tz=timezone.utc
                ).isoformat()
            except (OSError, OverflowError, ValueError):
                post_date = datetime.now(timezone.utc).isoformat()
        else:
            post_date = datetime.now(timezone.utc).isoformat()

    # Author is a nested object
    author: Dict[str, Any] = item.get("author") or {}
    author_name = author.get("name", "")
    author_title = author.get("info", "")        # "info" = headline/job title
    author_profile_url = author.get("linkedinUrl", "")

    # Engagement is a nested object: {"likes": 4, "comments": 0, "shares": 1}
    engagement: Dict[str, Any] = item.get("engagement") or {}
    likes_count = int(engagement.get("likes") or 0)
    comments_count = int(engagement.get("comments") or 0)

    return RawPost(
        post_url=post_url,
        author_name=author_name,
        author_title=author_title,
        author_profile_url=author_profile_url,
        post_text=post_text,
        post_date=post_date,
        likes_count=likes_count,
        comments_count=comments_count,
        contact_info=_extract_contact_info(post_text),
        country="",    # Country filtering delegated to Claude scorer
        keyword="",    # Actor does not report which query produced each post
    )
