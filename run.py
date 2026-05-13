"""
run.py — Entry point for the LinkedIn Freelance Mission Tracker.

Orchestrates the full pipeline:
  1. Load config (fail fast if env vars missing)
  2. Scrape LinkedIn posts via Apify (supreme_coder/linkedin-post actor)
  3. Score posts via Claude API
  4. Write results to Google Sheets

Invoked daily by GitHub Actions at 07:00 UTC (09:00 CEST, lun–sam).
Can also be run locally: `python run.py`
"""

import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


def setup_logging(date_str: str) -> logging.Logger:
    """
    Configure a logger that writes to both a daily log file and stdout.

    File handler: DEBUG level → logs/run_{date_str}.log
    Stream handler: INFO level → stdout

    Creates the logs/ directory if it does not exist.

    Args:
        date_str: Date string in YYYY-MM-DD format.

    Returns:
        Configured Logger instance.
    """
    logs_dir = Path(__file__).parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    log_path = logs_dir / f"run_{date_str}.log"

    logger = logging.getLogger("freelance_tracker")
    logger.setLevel(logging.DEBUG)

    # Avoid adding duplicate handlers on re-import
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    return logger




def main() -> None:
    """
    Run the full LinkedIn Freelance Mission Tracker pipeline.

    Steps:
      1. Setup logging
      2. Load .env (no-op in GitHub Actions where secrets are injected natively)
      3. Load and validate config (raises on missing env vars → exit 1)
      4. Load cross-run dedup index from Google Sheet
      5. Scrape posts via BeReach (known posts filtered before scoring)
      6. Score posts via Claude API
      7. Write to Google Sheets + update Dedup_Index
      8. Log final summary

    On any unhandled exception: logs full traceback and exits with code 1
    so GitHub Actions marks the run as failed.
    """
    pipeline_start = time.time()
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    run_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    logger = setup_logging(date_str)

    logger.info("=" * 60)
    logger.info("LinkedIn Freelance Mission Tracker — run started")
    logger.info("Date: %s UTC", date_str)
    logger.info("=" * 60)

    # Load .env for local development (no-op when env vars are already set)
    load_dotenv()

    run_mode = os.getenv("RUN_MODE", "freelance").strip().lower()
    logger.info("[run] RUN_MODE: %s", run_mode)

    try:
        # Import here so missing deps surface with a clear error after logging is set up
        from config.config import load_config
        from scraper import scrape_apify
        from matcher import score_posts, fetch_profile_vectors
        from sheets import write_missions, sync_config_tab, load_profile_vectors, save_profile_vectors, load_feedback_examples, load_seen_posts_all_tabs, index_rejected_posts
        from sheets.usage_stats import write_usage_stats

        # Step 1 — Config (fail fast)
        logger.info("[run] Loading configuration...")
        config = load_config()

        # Step 1b — Override config with Paramètres tab values (if tab exists)
        logger.info("[run] Syncing config with Google Sheets 'Paramètres' tab...")
        config = sync_config_tab(config, logger)

        # Guard: detect unfilled placeholder values after sheet override
        _placeholder_profiles = [
            p for p in config.linkedin_profiles
            if "YOUR_" in p.get("url", "") or "YOUR_" in p.get("name", "")
        ]
        if _placeholder_profiles:
            raise EnvironmentError(
                "Placeholder values detected in LINKEDIN_PROFILES. "
                "Open the 'Paramètres' tab in your Google Sheet and replace "
                "the YOUR_NAME / YOUR_LINKEDIN_PROFILE values with your real profile, "
                "then re-run."
            )

        logger.info(
            "[run] Config ready. Countries: %s | Keywords: %d | Min score: %d",
            config.target_countries,
            len(config.search_keywords),
            config.min_match_score,
        )

        # Step 1c — Load cached profile vectors from sheet; fetch missing via BeReach
        logger.info("[run] Loading profile vectors (from cache or BeReach)...")
        cached_vectors = load_profile_vectors(config, logger)
        profile_vectors = fetch_profile_vectors(config, logger, cached=cached_vectors)

        # Save any newly fetched vectors back to the sheet for future runs
        new_vectors = {url: info for url, info in profile_vectors.items() if url not in cached_vectors}
        if new_vectors:
            logger.info("[run] Saving %d new profile vector(s) to sheet cache...", len(new_vectors))
            save_profile_vectors(profile_vectors, config, logger)
        else:
            logger.info("[run] All profile vectors loaded from cache — no BeReach call needed.")

        # Step 2 — Load cross-run dedup index from Google Sheet
        logger.info("[run] Loading dedup index (cross-run deduplication)...")
        seen_urls_global, seen_hashes_global = load_seen_posts_all_tabs(config, logger)
        logger.info(
            "[run] Dedup index ready — %d known URLs, %d known text hashes.",
            len(seen_urls_global), len(seen_hashes_global),
        )

        # Step 3 — Apify scraper
        raw_posts = []
        logger.info("[run] Starting Apify scraping...")
        keyword_override = config.remote_keywords if run_mode == "job" else None
        apify_posts, apify_stats = scrape_apify(config, logger, seen_urls=seen_urls_global, seen_hashes=seen_hashes_global, keyword_override=keyword_override)
        raw_posts.extend(apify_posts)
        logger.info("[run] Apify scraping complete — %d posts collected.", len(apify_posts))

        logger.info("[run] Scraping complete — %d total raw posts collected.", len(raw_posts))

        if not raw_posts:
            logger.warning("[run] No posts scraped. Check Apify API token and keyword config.")

        # Step 3 — Score (load past user feedback to inject into Claude prompt)
        logger.info("[run] Loading user feedback examples from sheet...")
        feedback_examples = load_feedback_examples(config, logger)
        if feedback_examples:
            logger.info("[run] %d feedback example(s) will guide scoring.", len(feedback_examples))

        logger.info("[run] Starting Claude scoring...")
        enriched_posts = score_posts(
            raw_posts, config, logger,
            profile_vectors=profile_vectors,
            feedback_examples=feedback_examples,
            scoring_mode=run_mode,
        )
        logger.info(
            "[run] Scoring complete — %d posts scored >= %d.",
            len(enriched_posts), config.min_match_score,
        )

        # Step 3c — Location filter
        before_loc = len(enriched_posts)
        kept, dropped = [], []

        if run_mode == "job":
            # Remote pipeline: keep only posts that are truly location-independent
            # (worker can be anywhere worldwide — no residency or onsite constraint).
            for p in enriched_posts:
                if p.get("truly_location_independent", True):
                    kept.append(p)
                else:
                    dropped.append(p)
                    logger.info(
                        "[run] Remote filter DROP (not location-independent) — score=%.1f location='%s' url=%s",
                        p.get("match_score", 0), p.get("location", ""), p.get("post_url", ""),
                    )
            logger.info(
                "[run] Remote location filter: %d/%d posts kept (truly_location_independent=True).",
                len(kept), before_loc,
            )
        else:
            # Freelance pipeline: keep posts where Claude flagged is_target_location=True
            for p in enriched_posts:
                if p.get("is_target_location", True):
                    kept.append(p)
                else:
                    dropped.append(p)
                    logger.info(
                        "[run] Location filter DROP — score=%.1f location='%s' url=%s",
                        p.get("match_score", 0), p.get("location", ""), p.get("post_url", ""),
                    )
            logger.info(
                "[run] Location filter: %d/%d posts kept (target countries: %s).",
                len(kept), before_loc, config.target_countries,
            )

        enriched_posts = kept

        # Index dropped posts in Dedup_Index so they are not re-scraped/re-scored next run
        if dropped:
            logger.info("[run] Indexing %d location-filtered posts in dedup to prevent re-scoring...", len(dropped))
            index_rejected_posts(dropped, config, logger)

        # Step 4 — Write to Sheets (seen sets passed for belt-and-suspenders dedup)
        logger.info("[run] Writing to Google Sheets...")
        tab_name_override = config.remote_tab if run_mode == "job" else None
        write_missions(enriched_posts, config, logger, seen_urls=seen_urls_global, seen_hashes=seen_hashes_global, tab_name_override=tab_name_override)

        # Final summary
        pipeline_duration = round(time.time() - pipeline_start, 1)
        logger.info("=" * 60)
        logger.info(
            "[run] Pipeline complete. %d missions written to Google Sheets.",
            len(enriched_posts),
        )
        logger.info("[run] Total pipeline duration: %.1fs", pipeline_duration)
        logger.info("=" * 60)

        # Write usage stats for the dashboard
        usage_entry = {
            "timestamp": run_timestamp,
            "date": date_str,
            "run_mode": run_mode,
            "status": "success",
            "posts_raw": apify_stats.get("posts_raw", 0),
            "posts_unique": apify_stats.get("posts_unique", 0),
            "posts_scored": len(enriched_posts),
            "posts_written": len(enriched_posts),
            "keywords_count": apify_stats.get("keywords_count", 0),
            "apify_cost_usd": apify_stats.get("cost_usd", -1),
            "apify_duration_seconds": apify_stats.get("duration_seconds", 0),
            "pipeline_duration_seconds": pipeline_duration,
        }
        write_usage_stats(usage_entry, logger)

    except EnvironmentError as exc:
        logger.critical("[run] Configuration error — cannot proceed: %s", exc)
        _write_error_stats(run_timestamp, date_str, run_mode, pipeline_start, logger)
        sys.exit(1)

    except Exception as exc:  # noqa: BLE001
        logger.critical("[run] Unhandled exception: %s", exc)
        logger.critical(traceback.format_exc())
        _write_error_stats(run_timestamp, date_str, run_mode, pipeline_start, logger)
        sys.exit(1)


def _write_error_stats(
    timestamp: str, date: str, run_mode: str, pipeline_start: float, logger: logging.Logger
) -> None:
    """Write a failed run entry to docs/usage.json (best effort)."""
    try:
        from sheets.usage_stats import write_usage_stats
        write_usage_stats({
            "timestamp": timestamp,
            "date": date,
            "run_mode": run_mode,
            "status": "error",
            "posts_raw": 0,
            "posts_unique": 0,
            "posts_scored": 0,
            "posts_written": 0,
            "keywords_count": 0,
            "apify_cost_usd": -1,
            "apify_duration_seconds": 0,
            "pipeline_duration_seconds": round(time.time() - pipeline_start, 1),
        }, logger)
    except Exception:
        pass  # Never let stats writing crash the process


if __name__ == "__main__":
    main()
