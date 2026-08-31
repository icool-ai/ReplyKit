"""Env-backed settings consumed by ``mp_agent`` (SQLite + DashScope)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- LLM (align with ReplyKit DashScope / 通义) ---
DASHSCOPE_API_KEY = (
    os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
).strip()
DASHSCOPE_BASE_URL = os.getenv(
    "OPENAI_API_BASE",
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
).strip()
DASHSCOPE_MODEL = (
    os.getenv("CHAT_MODEL") or os.getenv("MP_AGENT_LLM_MODEL") or "qwen-plus"
).strip()

LLM_BASE_URL = os.getenv("MP_AGENT_LLM_BASE_URL", DASHSCOPE_BASE_URL).strip()
LLM_MODEL = os.getenv("MP_AGENT_LLM_MODEL", DASHSCOPE_MODEL).strip()
LLM_API_KEY = os.getenv("MP_AGENT_LLM_API_KEY", DASHSCOPE_API_KEY).strip() or "EMPTY"

ANALYSIS_LLM_BASE_URL = os.getenv(
    "MP_AGENT_ANALYSIS_LLM_BASE_URL", LLM_BASE_URL
).strip()
ANALYSIS_LLM_MODEL = os.getenv(
    "MP_AGENT_ANALYSIS_LLM_MODEL", LLM_MODEL
).strip()
ANALYSIS_LLM_API_KEY = os.getenv(
    "MP_AGENT_ANALYSIS_LLM_API_KEY", LLM_API_KEY
).strip() or "EMPTY"

# --- SQLite (unified with ReplyKit data/*.db) ---
_competitor_db = Path(
    os.getenv("COMPETITOR_DB_PATH", "./data/competitor.db")
)
if not _competitor_db.is_absolute():
    _competitor_db = _PROJECT_ROOT / _competitor_db
_competitor_db.parent.mkdir(parents=True, exist_ok=True)

# SQLAlchemy async URL; aiosqlite driver
DB_URL = os.getenv(
    "MP_AGENT_DB_URL",
    f"sqlite+aiosqlite:///{_competitor_db.as_posix()}",
).strip()

# --- Artifacts ---
_artifacts = Path(
    os.getenv("MP_AGENT_ARTIFACTS_DIR", "./data/competitor_artifacts")
)
if not _artifacts.is_absolute():
    _artifacts = _PROJECT_ROOT / _artifacts
_artifacts.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MP_AGENT_ARTIFACTS_DIR", str(_artifacts))

# --- Crawl / cache ---
CACHE_TTL_DAYS = int(os.getenv("MP_AGENT_CACHE_TTL_DAYS", "3"))

# --- Apify / FlareSolverr / FX (optional per platform) ---
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN", "").strip()
APIFY_API_TOKEN_2 = os.getenv("APIFY_API_TOKEN_2", APIFY_API_TOKEN).strip()
APIFY_ALIEXPRESS_ACTOR = os.getenv(
    "APIFY_ALIEXPRESS_ACTOR", "bkYbOC0TL11Z6lmBl"
).strip()
APIFY_OZON_ACTOR = os.getenv("APIFY_OZON_ACTOR", "").strip()
APIFY_ALLEGRO_ACTOR = os.getenv("APIFY_ALLEGRO_ACTOR", "").strip()
APIFY_TIKTOKSHOP_ACTOR = os.getenv("APIFY_TIKTOKSHOP_ACTOR", "").strip()
APIFY_CDISCOUNT_ACTOR = os.getenv("APIFY_CDISCOUNT_ACTOR", "").strip()
APIFY_TEMU_ACTOR = os.getenv("APIFY_TEMU_ACTOR", "").strip()

FLARESOLVERR_URL = os.getenv(
    "FLARESOLVERR_URL", "http://localhost:8191/v1"
).strip()
FLARESOLVERR_MAX_CONCURRENT = int(
    os.getenv("FLARESOLVERR_MAX_CONCURRENT", "3")
)

EUR_TO_USD = float(os.getenv("EUR_TO_USD", "1.08"))
RUB_TO_USD = float(os.getenv("RUB_TO_USD", "0.011"))
PLN_TO_USD = float(os.getenv("PLN_TO_USD", "0.25"))

# Amazon review xlsx bridge (optional local paths)
ASIN_LIST_XLSX_PATH = os.getenv("ASIN_LIST_XLSX_PATH", "").strip() or str(
    _PROJECT_ROOT / "data" / "competitor_artifacts" / "asin_list.xlsx"
)
ALL_REVIEWS_XLSX_PATH = os.getenv("ALL_REVIEWS_XLSX_PATH", "").strip() or str(
    _PROJECT_ROOT / "data" / "competitor_artifacts" / "all_reviews.xlsx"
)
XLSX_POLL_INTERVAL_SEC = float(os.getenv("XLSX_POLL_INTERVAL_SEC", "2"))
XLSX_POLL_TIMEOUT_SEC = float(os.getenv("XLSX_POLL_TIMEOUT_SEC", "120"))

# --- Redis (optional: JWT blacklist + login rate limit) ---
REDIS_ENABLED = (
    os.getenv("REDIS_ENABLED", "true").strip().lower()
    in {"1", "true", "yes", "on"}
)
REDIS_URL = os.getenv("REDIS_URL", os.getenv("MP_AGENT_REDIS_URL", "")).strip()
LOGIN_RATE_LIMIT = int(os.getenv("LOGIN_RATE_LIMIT", "5"))
LOGIN_RATE_WINDOW_SEC = int(os.getenv("LOGIN_RATE_WINDOW_SEC", "300"))
