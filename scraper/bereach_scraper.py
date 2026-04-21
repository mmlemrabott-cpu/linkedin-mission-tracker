"""
scraper/bereach_scraper.py — BeReach API LinkedIn post scraper.

Rate-limit design (per official BeReach API v1.5.0 + empirical behaviour):
  - BeReach enforces ~2 requests per 5-minute sliding window per token.
  - Keyword queries run in BATCHES of _RATE_LIMIT_BATCH_SIZE (2).
  - A _RATE_LIMIT_INTRA_BATCH_DELAY random pause separates the two queries
    within each batch.
  - A _RATE_LIMIT_BATCH_PAUSE (310s) separates consecutive batches, ensuring
    the 5-minute window from the previous batch is fully reset before the next
    batch starts.
  - Within a single multi-page query the `retryAfter` field returned by every
    200 response controls inter-page pacing.
  - HTTP 429 responses include `error.retryAfter` (int, seconds) — the exact
    wait time before retrying. When absent or zero, an exponential fallback
    (30s→60s→120s) is used.

Each query paginates while hasMore is True (up to max_posts_per_country),
normalises results into RawPost dicts, applies a 24h safety filter,
deduplicates by URL and text hash, saves raw JSON to disk, and returns the
merged final list.

Keywords are sent to BeReach as-is (no country suffix). Country filtering is
handled downstream by Claude via the is_target_location field.
"""

import json
import logging
import random
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

# BeReach API base URL and endpoint (official domain per API docs v1.5.0)
_BASE_URL = "https://api.bereach.ai"
_ENDPOINT = "/search/linkedin/posts"

# Results per page (BeReach max is 50)
_PAGE_SIZE = 50

# HTTP timeout in seconds
_REQUEST_TIMEOUT = 30

# ── Rate-limit constants (per CLAUDE.md) ────────────────────────────────────
# BeReach enforces ~2 requests per 5-minute sliding window.
# Queries run in batches of _RATE_LIMIT_BATCH_SIZE with a long pause between
# batches and a short random delay between the two queries in each batch.
_RATE_LIMIT_BATCH_SIZE = 2           # queries per batch
_RATE_LIMIT_BATCH_PAUSE = 310        # seconds to wait between batches
_RATE_LIMIT_INTRA_BATCH_DELAY = (3, 6)   # (min, max) seconds within a batch
# ────────────────────────────────────────────────────────────────────────────

# Maximum retry attempts on HTTP 429 before giving up on a single page request
_MAX_429_RETRIES = 3

# Fallback backoff (seconds) when the 429 response body contains no retryAfter.
# Doubles each attempt: 30s → 60s → 120s.
_BACKOFF_BASE = 30.0

# Safety margin (seconds) added on top of the API-provided retryAfter value to
# account for clock skew and network latency (used for 429 retry waits only).
_RETRY_AFTER_MARGIN = 3


def scrape_bereach(
    config: AppConfig,
    logger: logging.Logger,
    seen_urls: Optional[Set[str]] = None,
    seen_hashes: Optional[Set[str]] = None,
    keyword_override: Optional[List[str]] = None,
) -> List[RawPost]:
    """
    Fetch LinkedIn posts from the BeReach API for all keywords in config.

    Queries run in batches of _RATE_LIMIT_BATCH_SIZE (2) with a
    _RATE_LIMIT_INTRA_BATCH_DELAY between queries inside each batch and a
    _RATE_LIMIT_BATCH_PAUSE (310s) between consecutive batches.  This respects
    the BeReach ~2-requests-per-5-minute-window rate limit.

    Each query paginates while hasMore is True or until max_posts_per_country
    is reached. Results are merged and deduplicated by URL and text hash
    (within-run and cross-run). Saves raw results to
    data/raw_posts_{YYYY-MM-DD}.json.

    Keywords are sent to BeReach exactly as written in config — no country
    suffix is appended. Country filtering is delegated to Claude
    (is_target_location field).

    Args:
        config: Application configuration (provides bereach_api_token,
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

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    headers = {
        "Authorization": f"Bearer {config.bereach_api_token}",
        "Content-Type": "application/json",
    }

    num_batches = (len(keyword_queries) + _RATE_LIMIT_BATCH_SIZE - 1) // _RATE_LIMIT_BATCH_SIZE
    logger.info(
        "[bereach] Running %d keyword queries in %d batch(es) of %d "
        "(intra-batch delay %d–%ds, inter-batch pause %ds).",
        len(keyword_queries),
        num_batches,
        _RATE_LIMIT_BATCH_SIZE,
        _RATE_LIMIT_INTRA_BATCH_DELAY[0],
        _RATE_LIMIT_INTRA_BATCH_DELAY[1],
        _RATE_LIMIT_BATCH_PAUSE,
    )

    raw_batches: List[List[Dict[str, Any]]] = []

    for batch_idx in range(0, len(keyword_queries), _RATE_LIMIT_BATCH_SIZE):
        batch = keyword_queries[batch_idx: batch_idx + _RATE_LIMIT_BATCH_SIZE]
        batch_num = batch_idx // _RATE_LIMIT_BATCH_SIZE + 1

        logger.info(
            "[bereach] Batch %d/%d — %d query(ies).",
            batch_num,
            num_batches,
            len(batch),
        )

        for i, keywords in enumerate(batch):
            try:
                items, _ = _fetch_all_pages(
                    keywords, headers, config.max_posts_per_country, logger
                )
                raw_batches.append(items)
            except Exception as exc:
                logger.error(
                    "[bereach] Query failed — keywords='%.60s...': %s", keywords, exc
                )
                raw_batches.append([])

            # Intra-batch delay between the two queries (not after the last one)
            if i < len(batch) - 1:
                delay = random.uniform(*_RATE_LIMIT_INTRA_BATCH_DELAY)
                logger.debug(
                    "[bereach] Intra-batch delay %.1fs before next query in batch.",
                    delay,
                )
                time.sleep(delay)

        # Inter-batch pause (not after the last batch)
        is_last_batch = (batch_idx + _RATE_LIMIT_BATCH_SIZE) >= len(keyword_queries)
        if not is_last_batch:
            logger.info(
                "[bereach] Batch %d complete — waiting %ds before next batch "
                "(rate-limit window reset).",
                batch_num,
                _RATE_LIMIT_BATCH_PAUSE,
            )
            time.sleep(_RATE_LIMIT_BATCH_PAUSE)

    # Merge and deduplicate
    seen_urls_run: Set[str] = set()
    seen_text_hashes_run: Set[str] = set()
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
                logger.debug(
                    "[bereach] near-duplicate text skipped: %s", post["post_url"]
                )
                continue
            if post["post_url"] in seen_urls_global:
                logger.debug(
                    "[bereach] cross-run duplicate URL skipped: %s", post["post_url"]
                )
                continue
            if text_hash in seen_hashes_global:
                logger.debug(
                    "[bereach] cross-run repost skipped: %s", post["post_url"]
                )
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
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Fetch all paginated results for a single keyword query from the BeReach API.

    Paginates while hasMore is True and the collected item count is below
    max_posts. The `retryAfter` field from each 200 response controls the
    inter-page delay within this query (pagination pacing). Returns both the
    collected items and the last retryAfter value.

    Args:
        keywords: Boolean keyword query string.
        headers: HTTP headers including Authorization.
        max_posts: Maximum number of raw items to collect.
        logger: Logger instance.

    Returns:
        Tuple of (list of raw item dicts, retryAfter seconds from last response).
    """
    collected: List[Dict[str, Any]] = []
    start = 0
    page = 0
    last_retry_after = 0

    while len(collected) < max_posts:
        if page > 0:
            # Inter-page delay: respect retryAfter from previous page response
            # (pagination pacing within a single query — separate from the
            # inter-keyword batch pacing handled by scrape_bereach).
            page_delay = last_retry_after + _RETRY_AFTER_MARGIN if last_retry_after > 0 else random.uniform(1, 3)
            logger.debug(
                "[bereach] Inter-page delay %.1fs (page %d).", page_delay, page
            )
            time.sleep(page_delay)

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
        last_retry_after = int(data.get("retryAfter") or 0)

        logger.info(
            "[bereach] keywords='%.60s...' start=%d → %d items "
            "(hasMore=%s, credits=%s, retryAfter=%ds)",
            keywords, start, len(items), has_more, credits_used, last_retry_after,
        )

        collected.extend(items)

        if not has_more or not items:
            break

        start += _PAGE_SIZE
        page += 1

    return collected, last_retry_after


def _post_with_retry(
    keywords: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    logger: logging.Logger,
) -> Optional[Dict[str, Any]]:
    """
    POST to the BeReach API with retry on HTTP 429.

    On HTTP 429 the response body is parsed for `error.retryAfter` (the exact
    number of seconds to wait, per official BeReach API docs). If absent or
    zero, a fallback exponential backoff is used (30s → 60s → 120s). Non-429
    errors abort immediately.

    Args:
        keywords: Keyword query string (used only for log messages).
        payload: JSON request body.
        headers: HTTP headers including Authorization.
        logger: Logger instance.

    Returns:
        Parsed JSON response dict, or None if all attempts failed.
    """
    start_offset = payload.get("start", 0)

    for attempt in range(1, _MAX_429_RETRIES + 2):  # 1 initial + up to 3 retries
        try:
            logger.debug(
                "[bereach] POST %s%s keywords='%.60s...' start=%d (attempt %d)",
                _BASE_URL, _ENDPOINT, keywords, start_offset, attempt,
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
                # Parse retryAfter from the 429 response body (official BeReach mechanism)
                retry_after_api = 0
                body_str = ""
                try:
                    body = exc.response.json()
                    body_str = json.dumps(body)
                    retry_after_api = int(
                        (body.get("error") or {}).get("retryAfter") or 0
                    )
                except Exception as parse_err:
                    try:
                        body_str = exc.response.text[:300]
                    except Exception:
                        body_str = "<unreadable>"

                logger.debug(
                    "[bereach] HTTP 429 raw body (keywords='%.40s...', start=%d): %s",
                    keywords, start_offset, body_str[:300],
                )

                if retry_after_api > 0:
                    wait = retry_after_api + _RETRY_AFTER_MARGIN
                    logger.warning(
                        "[bereach] HTTP 429 (keywords='%.60s...', start=%d) — "
                        "API retryAfter=%ds, waiting %ds (retry %d/%d).",
                        keywords, start_offset,
                        retry_after_api, wait, attempt, _MAX_429_RETRIES,
                    )
                else:
                    wait = _BACKOFF_BASE * (2 ** (attempt - 1))
                    logger.warning(
                        "[bereach] HTTP 429 (keywords='%.60s...', start=%d) — "
                        "no retryAfter in body, fallback backoff %.0fs (retry %d/%d).",
                        keywords, start_offset, wait, attempt, _MAX_429_RETRIES,
                    )

                time.sleep(wait)
                continue

            logger.error(
                "[bereach] HTTP %d error (keywords='%.60s...', start=%d): %s",
                status, keywords, start_offset, exc,
            )
            return None

        except Exception as exc:
            logger.error(
                "[bereach] Request failed (keywords='%.60s...', start=%d): %s",
                keywords, start_offset, exc,
            )
            return None

    logger.error(
        "[bereach] Gave up after %d retries on 429 (keywords='%.60s...', start=%d)",
        _MAX_429_RETRIES, keywords, start_offset,
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
            post_date = datetime.fromtimestamp(
                raw_date / 1000, tz=timezone.utc
            ).isoformat()
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
