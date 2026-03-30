"""
Alias learner — caches AI-generated keyword synonyms locally.

Learning is triggered:
  1. When a new keyword is added (immediate, background)
  2. On a periodic refresh (daily/weekly, background)
  3. When a job in an unexpected language is found (background, that language only)

Cache lives in data/alias_cache.json:
{
  "keywords": {
    "Software Engineer": {
      "en": ["Developer", "Programmer", "SWE"],
      "zh": ["软件工程师", "开发工程师"],
      "last_updated": "2026-03-22"
    }
  }
}
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from threading import Thread, Lock

logger = logging.getLogger(__name__)

CACHE_FILE = "alias_cache.json"
_CACHE_LOCK = Lock()

LOCATION_LANG_MAP = {
    "united kingdom": "en", "uk": "en", "england": "en", "britain": "en",
    "united states": "en", "usa": "en", "canada": "en",
    "australia": "en", "new zealand": "en", "ireland": "en",
    "china": "zh", "hong kong": "zh", "taiwan": "zh", "singapore": "zh",
    "france": "fr", "belgium": "fr",
    "germany": "de", "austria": "de",
    "italy": "it",
    "spain": "es", "mexico": "es", "argentina": "es",
    "portugal": "pt", "brazil": "pt",
    "netherlands": "nl",
    "japan": "ja",
    "korea": "ko", "south korea": "ko",
}

LANG_NAMES = {
    "en": "English", "zh": "Chinese",
    "fr": "French", "de": "German", "it": "Italian",
    "es": "Spanish", "pt": "Portuguese", "nl": "Dutch",
    "ja": "Japanese", "ko": "Korean",
}


# ── Language detection ───────────────────────────────────────

def detect_languages_from_location(location: str) -> list[str]:
    """Infer relevant languages from search location string."""
    langs = ["en"]
    if not location:
        return langs
    loc = location.lower()
    for key, lang in LOCATION_LANG_MAP.items():
        if key in loc:
            if lang not in langs:
                langs.append(lang)
    return langs


# ── Cache I/O ────────────────────────────────────────────────

def load_cache(data_dir: str) -> dict:
    path = Path(data_dir) / CACHE_FILE
    if not path.exists():
        return {"keywords": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"keywords": {}}


def save_cache(data_dir: str, cache: dict) -> None:
    path = Path(data_dir) / CACHE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Staleness check ──────────────────────────────────────────

def _is_stale(last_updated: str, refresh_days: int) -> bool:
    if not last_updated:
        return True
    try:
        return (date.today() - date.fromisoformat(last_updated)).days >= refresh_days
    except Exception:
        return True


# ── AI learning ──────────────────────────────────────────────

def _call_ai_for_aliases(keyword: str, languages: list[str],
                          provider: str, api_key: str, model: str) -> dict[str, list[str]]:
    """Ask AI for aliases of keyword in given languages. Returns {lang_code: [aliases]}."""
    from matcher import _call_ai

    lang_list = ", ".join(LANG_NAMES.get(l, l) for l in languages)
    system = (
        f"You are a job search expert. Given a job title keyword, generate synonyms and common "
        f"alternative titles in these languages only: {lang_list}.\n\n"
        "Rules:\n"
        "1. Cover common alternative job titles for the same role\n"
        "2. Include abbreviations where common (PM, SWE, RN, etc.)\n"
        "3. Max 5 aliases per language — quality over quantity\n"
        "4. Do NOT include the original keyword itself\n"
        f"5. Return ONLY valid JSON with these exact language codes: {json.dumps(languages)}\n"
        "Example: {\"en\": [\"Developer\", \"Programmer\"], \"zh\": [\"开发工程师\"]}"
    )
    user = f"Job keyword: {keyword}"

    result_text = _call_ai(provider, api_key, model, system, user)
    clean = re.sub(r"```json|```", "", result_text).strip()
    data = json.loads(clean)
    return {lang: [str(a) for a in data.get(lang, [])[:5]] for lang in languages}


def _learn_and_cache(data_dir: str, keyword: str, languages: list[str],
                      provider: str, api_key: str, model: str) -> None:
    """Learn aliases for keyword and update cache. Safe to call from background thread."""
    try:
        learned = _call_ai_for_aliases(keyword, languages, provider, api_key, model)
        with _CACHE_LOCK:
            cache = load_cache(data_dir)
            if keyword not in cache["keywords"]:
                cache["keywords"][keyword] = {}
            for lang, aliases in learned.items():
                cache["keywords"][keyword][lang] = aliases
            cache["keywords"][keyword]["last_updated"] = date.today().isoformat()
            save_cache(data_dir, cache)
        total = sum(len(v) for v in learned.values())
        logger.info(f"别名学习完成：「{keyword}」→ {total} 个别名 ({', '.join(languages)})")
    except Exception as e:
        logger.warning(f"别名学习失败 [{keyword}]：{e}")


# ── Public API ───────────────────────────────────────────────

def trigger_learning(data_dir: str, keyword: str, languages: list[str],
                      provider: str, api_key: str, model: str) -> None:
    """Trigger background learning for a single keyword (called on keyword add)."""
    if not api_key:
        return
    Thread(
        target=_learn_and_cache,
        args=(data_dir, keyword, languages, provider, api_key, model),
        daemon=True,
    ).start()


def refresh_stale_keywords(data_dir: str, keywords: list[str], languages: list[str],
                            refresh_days: int, provider: str, api_key: str, model: str) -> None:
    """Refresh keywords whose cache is older than refresh_days. Runs in background."""
    if not api_key or not keywords:
        return

    cache = load_cache(data_dir)
    to_refresh = []
    for kw in keywords:
        kw_data = cache["keywords"].get(kw, {})
        last = kw_data.get("last_updated", "")
        missing_langs = [l for l in languages if l not in kw_data]
        if _is_stale(last, refresh_days) or missing_langs:
            to_refresh.append(kw)

    if not to_refresh:
        return

    def _run():
        for kw in to_refresh:
            _learn_and_cache(data_dir, kw, languages, provider, api_key, model)

    logger.info(f"后台刷新别名：{to_refresh}")
    Thread(target=_run, daemon=True).start()


def learn_unexpected_language(data_dir: str, lang: str, keywords: list[str],
                               provider: str, api_key: str, model: str) -> None:
    """Called when a job in an unexpected language is found. Learns that language only."""
    if not api_key:
        return

    def _run():
        cache = load_cache(data_dir)
        for kw in keywords:
            if lang not in cache["keywords"].get(kw, {}):
                _learn_and_cache(data_dir, kw, [lang], provider, api_key, model)

    logger.info(f"发现新语言 [{lang}]，追加学习...")
    Thread(target=_run, daemon=True).start()


def get_all_aliases(data_dir: str, keyword: str, languages: list[str]) -> list[str]:
    """Return cached aliases for keyword in the given languages."""
    cache = load_cache(data_dir)
    kw_data = cache["keywords"].get(keyword, {})
    aliases = []
    for lang in languages:
        aliases.extend(kw_data.get(lang, []))
    return list(dict.fromkeys(aliases))  # deduplicate, preserve order


def expand_keywords(data_dir: str, keywords: list[str], user_aliases: dict,
                    languages: list[str], use_cache: bool) -> list[str]:
    """
    Build the final search term list:
      1. Original keywords
      2. User-defined aliases (manual, always applied)
      3. AI-cached aliases (only if use_cache=True)
    """
    seen = set()
    result = []

    for kw in keywords:
        kw_clean = kw.strip()
        if kw_clean.lower() not in seen:
            seen.add(kw_clean.lower())
            result.append(kw_clean)

        for alias in user_aliases.get(kw_clean, []):
            a = alias.strip()
            if a.lower() not in seen:
                seen.add(a.lower())
                result.append(a)

        if use_cache:
            for alias in get_all_aliases(data_dir, kw_clean, languages):
                if alias.lower() not in seen:
                    seen.add(alias.lower())
                    result.append(alias)

    return result
