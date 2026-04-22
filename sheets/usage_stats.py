"""
sheets/usage_stats.py — Pipeline usage statistics writer.

Appends one entry per pipeline run to docs/usage.json, which is committed
to the repo by the GitHub Actions workflow and served by GitHub Pages as the
data source for the usage dashboard (docs/index.html).

The file is capped at the last 90 entries (~3 months of daily runs for both
freelance and job pipelines combined) to prevent unbounded growth.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict


# Path relative to repo root
_USAGE_JSON_PATH = Path(__file__).parent.parent / "docs" / "usage.json"

# Maximum number of entries to retain (older entries are dropped)
_MAX_ENTRIES = 90


def write_usage_stats(entry: Dict[str, Any], logger: logging.Logger) -> None:
    """
    Append one run entry to docs/usage.json, creating the file if absent.

    The entry dict should contain:
        timestamp (str)              — ISO 8601 UTC of the run start
        date (str)                   — YYYY-MM-DD
        run_mode (str)               — "freelance" or "job"
        status (str)                 — "success" or "error"
        posts_raw (int)              — posts returned by Apify
        posts_unique (int)           — after 24h filter + dedup
        posts_scored (int)           — after Claude scoring (>= min_score)
        posts_written (int)          — written to Google Sheets
        keywords_count (int)         — number of keyword queries
        apify_cost_usd (float)       — real cost from Apify billing (-1 = unavailable)
        apify_duration_seconds (float) — Apify actor call wall time
        pipeline_duration_seconds (float) — total pipeline wall time

    Entries are sorted by timestamp (oldest first) and capped at _MAX_ENTRIES.

    Args:
        entry: Dict of metrics for this run.
        logger: Logger instance.
    """
    _USAGE_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)

    existing: list = []
    if _USAGE_JSON_PATH.exists():
        try:
            existing = json.loads(_USAGE_JSON_PATH.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                logger.warning(
                    "[usage_stats] docs/usage.json is not a list — resetting to []."
                )
                existing = []
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "[usage_stats] Could not read docs/usage.json (%s) — starting fresh.", exc
            )
            existing = []

    existing.append(entry)

    # Keep only the most recent _MAX_ENTRIES entries
    if len(existing) > _MAX_ENTRIES:
        existing = existing[-_MAX_ENTRIES:]

    try:
        _USAGE_JSON_PATH.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(
            "[usage_stats] docs/usage.json updated (%d entries total).", len(existing)
        )
    except OSError as exc:
        logger.error("[usage_stats] Failed to write docs/usage.json: %s", exc)
