"""
config/config.py — Single source of truth for all configuration.

Loads and validates environment variables and config/settings.json at startup.
Every other module receives an AppConfig instance; no module calls os.getenv() directly.
"""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class AppConfig:
    """Fully validated application configuration."""

    # From environment variables
    anthropic_api_key: str
    google_service_account_json: str  # raw JSON string, parsed in-memory — never written to disk
    spreadsheet_id: str
    apify_api_token: str

    # From config/settings.json (or overridden by Paramètres sheet tab)
    linkedin_profiles: List[Dict[str, str]]  # [{"name": "...", "url": "..."}, ...]  max 3
    target_countries: List[str]
    search_keywords: List[str]
    min_match_score: int
    max_posts_per_country: int
    sheet_tab_format: str
    remote_keywords: List[str]   # keywords used when RUN_MODE=job
    remote_tab: str              # sheet tab name for remote job results



def load_config() -> AppConfig:
    """
    Load and validate all configuration from environment variables and
    config/settings.json. Raises EnvironmentError if any required env var
    is missing or invalid. Raises FileNotFoundError if settings.json is absent.

    Returns:
        AppConfig: Fully populated and validated configuration dataclass.
    """
    settings = _load_settings_json(
        str(Path(__file__).parent / "settings.json")
    )

    anthropic_api_key = _require_env("ANTHROPIC_API_KEY")
    google_service_account_json = _require_env("GOOGLE_SERVICE_ACCOUNT_JSON")
    spreadsheet_id = _require_env("SPREADSHEET_ID")
    apify_api_token = _require_env("APIFY_API_TOKEN")

    # Validate that the service account JSON is parseable before any API calls are made
    try:
        json.loads(google_service_account_json)
    except json.JSONDecodeError as exc:
        raise EnvironmentError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON. "
            "Check that the value is not truncated or improperly escaped."
        ) from exc

    min_match_score = int(settings.get("MIN_MATCH_SCORE", 40))
    if not 0 <= min_match_score <= 100:
        raise EnvironmentError(
            f"MIN_MATCH_SCORE must be between 0 and 100, got {min_match_score}."
        )

    # Support both new LINKEDIN_PROFILES list and legacy MY_LINKEDIN_URL string
    if "LINKEDIN_PROFILES" in settings:
        linkedin_profiles = settings["LINKEDIN_PROFILES"]
        if not isinstance(linkedin_profiles, list) or not linkedin_profiles:
            raise EnvironmentError("LINKEDIN_PROFILES must be a non-empty list in settings.json.")
        for p in linkedin_profiles:
            if not isinstance(p, dict) or "name" not in p or "url" not in p:
                raise EnvironmentError(
                    "Each entry in LINKEDIN_PROFILES must have 'name' and 'url' keys."
                )
    elif "MY_LINKEDIN_URL" in settings:
        # Backward-compat with old settings.json format
        linkedin_profiles = [{"name": "Principal", "url": settings["MY_LINKEDIN_URL"]}]
    else:
        raise EnvironmentError(
            "settings.json must contain either 'LINKEDIN_PROFILES' (list) or 'MY_LINKEDIN_URL' (string)."
        )

    if not settings.get("TARGET_COUNTRIES"):
        raise EnvironmentError("TARGET_COUNTRIES is empty or missing in settings.json.")
    if not settings.get("SEARCH_KEYWORDS"):
        raise EnvironmentError("SEARCH_KEYWORDS is empty or missing in settings.json.")

    if len(settings["SEARCH_KEYWORDS"]) > 6:
        logging.getLogger(__name__).warning(
            "SEARCH_KEYWORDS has %d entries; only the first 6 will be shown in the Paramètres sheet.",
            len(settings["SEARCH_KEYWORDS"]),
        )

    return AppConfig(
        anthropic_api_key=anthropic_api_key,
        google_service_account_json=google_service_account_json,
        spreadsheet_id=spreadsheet_id,
        linkedin_profiles=linkedin_profiles[:3],  # max 3 profiles
        target_countries=settings["TARGET_COUNTRIES"],
        search_keywords=settings["SEARCH_KEYWORDS"],
        min_match_score=min_match_score,
        max_posts_per_country=int(settings.get("MAX_POSTS_PER_COUNTRY", 50)),
        sheet_tab_format=settings.get("SHEET_TAB_FORMAT", "Missions_{YYYY-MM}"),
        remote_keywords=settings.get("REMOTE_KEYWORDS", []),
        remote_tab=settings.get("REMOTE_TAB", "Remote"),
        apify_api_token=apify_api_token,
    )


def _load_settings_json(path: str) -> Dict[str, Any]:
    """
    Read and parse config/settings.json.

    Args:
        path: Absolute path to settings.json.

    Returns:
        Dict containing raw settings data.

    Raises:
        FileNotFoundError: If settings.json does not exist at path.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    settings_path = Path(path)
    if not settings_path.exists():
        raise FileNotFoundError(
            f"settings.json not found at {path}. "
            "Copy config/settings.json and fill in your values."
        )
    with open(settings_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _require_env(key: str) -> str:
    """
    Fetch an environment variable or raise EnvironmentError.

    Args:
        key: The name of the environment variable.

    Returns:
        The non-empty string value of the variable.

    Raises:
        EnvironmentError: If the variable is missing or empty.
    """
    value = os.getenv(key, "").strip()
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{key}' is missing or empty. "
            "Set it in .env (local) or GitHub Actions Secrets (CI)."
        )
    return value
