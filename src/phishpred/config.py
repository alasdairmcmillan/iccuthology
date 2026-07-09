"""Central paths and configuration."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "phish.db"

BASE_URL = "https://api.phish.net/v5"
PHISH_ARTIST_SLUG = "phish"

# Era boundaries by show year (total mapping — every year lands somewhere).
ERAS = [
    ("1.0", 0, 1996),
    ("2.0", 1997, 2000),
    ("2.5", 2001, 2008),
    ("3.0", 2009, 2020),
    ("4.0", 2021, 9999),
]


def era_for_year(year: int) -> str:
    for name, lo, hi in ERAS:
        if lo <= year <= hi:
            return name
    raise ValueError(f"no era for year {year}")


def _load_env() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv(PROJECT_ROOT / ".env.local", override=True)


def get_api_key() -> str:
    _load_env()
    key = os.getenv("PHISHNET_API_KEY")
    if not key:
        raise RuntimeError(
            "PHISHNET_API_KEY is not set. Copy .env.example to .env (or .env.local) "
            "and add a key from https://phish.net/api/keys"
        )
    return key


def get_app_id() -> str | None:
    _load_env()
    return os.getenv("PHISHNET_APP_ID")
