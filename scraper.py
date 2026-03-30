from __future__ import annotations

import json
import logging
import time
import requests
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    return json.loads(config_path.read_text())


def get_locations(config: dict) -> list[dict]:
    """
    Returns list of location configs.

    Supports two formats:
      - New: search.locations (array, each with own keywords + user_prompt)
      - Old: search.location (single string, falls back to global keywords/user_prompt)
    """
    search = config["search"]

    if "locations" in search and search["locations"]:
        result = []
        for entry in search["locations"]:
            if not entry.get("enabled", True):
                continue
            city = entry.get("city", "").strip()
            country = entry.get("country", "").strip()
            location = f"{city}, {country}" if city and country else entry.get("location", "")
            result.append({
                "name": entry.get("name", location),
                "location": location,
                "radius_km": entry.get("radius_km", search.get("radius_km", 30)),
                "keywords": entry.get("keywords", search.get("keywords", [])),
                "keyword_aliases": entry.get("keyword_aliases", search.get("keyword_aliases", {})),
                "user_prompt": entry.get("user_prompt", ""),
                "blacklist": entry.get("blacklist", {"companies": [], "keywords": []}),
            })
        if not result:
            raise ValueError("locations 列表中没有启用的地区（enabled: true），请检查配置")
        return result

    # Backward compat: old single-location format
    if search.get("location_mode") == "manual":
        loc = search.get("location", "").strip()
        if not loc:
            raise ValueError("手动位置模式下 location 不能为空，请在配置页填写城市名")
        return [{
            "name": loc,
            "location": loc,
            "radius_km": search.get("radius_km", 30),
            "keywords": search.get("keywords", []),
            "keyword_aliases": search.get("keyword_aliases", {}),
            "user_prompt": config.get("matching", {}).get("ai", {}).get("user_prompt", ""),
        }]

    # Auto: IP geolocation
    try:
        res = requests.get("https://ipapi.co/json/", timeout=10)
        data = res.json()
        city = data.get("city", "")
        country = data.get("country_name", "")
        if city:
            loc = f"{city}, {country}"
            return [{
                "name": loc,
                "location": loc,
                "radius_km": search.get("radius_km", 30),
                "keywords": search.get("keywords", []),
                "keyword_aliases": search.get("keyword_aliases", {}),
                "user_prompt": config.get("matching", {}).get("ai", {}).get("user_prompt", ""),
            }]
        raise ValueError("IP 定位返回空城市")
    except Exception as e:
        raise RuntimeError(f"⚠️ 无法自动获取位置：{e}，请在配置页切换为手动填入城市名")


def apply_blacklist(jobs: list[dict], config: dict,
                    loc_blacklist: Optional[dict] = None) -> list[dict]:
    global_bl = config["search"].get("blacklist", {})
    banned_companies = [c.lower() for c in global_bl.get("companies", [])]
    banned_keywords = [k.lower() for k in global_bl.get("keywords", [])]

    if loc_blacklist:
        banned_companies += [c.lower() for c in loc_blacklist.get("companies", [])]
        banned_keywords += [k.lower() for k in loc_blacklist.get("keywords", [])]

    filtered = []
    for job in jobs:
        company = job.get("company", "").lower()
        title = job.get("title", "").lower()

        if any(bc in company for bc in banned_companies):
            continue
        if any(bk in title for bk in banned_keywords):
            continue
        filtered.append(job)

    return filtered


def _scrape_one_location(loc_config: dict, global_config: dict, attempt: int = 1) -> list[dict]:
    """Scrape jobs for a single location config."""
    try:
        from jobspy import scrape_jobs
    except ImportError:
        raise RuntimeError("python-jobspy 未安装，请运行: pip install python-jobspy")

    search = global_config["search"]
    retry = search.get("retry", {})
    max_attempts = retry.get("max_attempts", 3)
    retry_interval = retry.get("retry_interval_minutes", 5)

    location = loc_config["location"]
    keywords = loc_config["keywords"]
    keyword_aliases = loc_config.get("keyword_aliases", {})

    al_cfg = global_config.get("alias_learning", {})
    al_enabled = al_cfg.get("enabled", False)
    data_dir = str(Path(__file__).parent / "data")

    from alias_learner import (
        detect_languages_from_location, expand_keywords,
        refresh_stale_keywords, learn_unexpected_language,
    )
    languages = detect_languages_from_location(location)

    if al_enabled and al_cfg.get("api_key"):
        refresh_stale_keywords(
            data_dir, keywords, languages,
            al_cfg.get("refresh_days", 7),
            al_cfg["provider"], al_cfg["api_key"], al_cfg.get("model", ""),
        )

    all_terms = expand_keywords(
        data_dir, keywords,
        keyword_aliases,
        languages,
        use_cache=al_enabled,
    )
    logger.info(f"[{loc_config['name']}] 抓取中：{all_terms}（第{attempt}次尝试）")

    try:
        raw = scrape_jobs(
            site_name=["linkedin"],
            search_term=" OR ".join(all_terms),
            location=location,
            distance=loc_config.get("radius_km", 30),
            hours_old=search.get("hours_old", 24),
            results_wanted=50,
        )

        jobs = []
        for _, row in raw.iterrows():
            link = str(row.get("job_url", "") or "")
            if not link:
                continue
            jobs.append({
                "title": str(row.get("title", "") or ""),
                "company": str(row.get("company", "") or ""),
                "location": str(row.get("location", "") or ""),
                "link": link,
                "posted_at": str(row.get("date_posted", "") or ""),
                "description": str(row.get("description", "") or ""),
                "source": "linkedin",
                # Carry location-specific context for the matcher
                "_user_prompt": loc_config["user_prompt"],
                "_location_name": loc_config["name"],
            })

        jobs = apply_blacklist(jobs, global_config, loc_config.get("blacklist"))
        logger.info(f"[{loc_config['name']}] 抓取完成，共 {len(jobs)} 条（过滤黑名单后）")

        if al_enabled and al_cfg.get("api_key"):
            seen_unexpected: set[str] = set()
            for job in jobs:
                for lang in detect_languages_from_location(job.get("location", "")):
                    if lang not in languages and lang not in seen_unexpected:
                        seen_unexpected.add(lang)
                        learn_unexpected_language(
                            data_dir, lang, keywords,
                            al_cfg["provider"], al_cfg["api_key"], al_cfg.get("model", ""),
                        )

        return jobs

    except Exception as e:
        if attempt < max_attempts:
            logger.warning(f"[{loc_config['name']}] 抓取失败（第{attempt}次）：{e}，{retry_interval}分钟后重试")
            time.sleep(retry_interval * 60)
            return _scrape_one_location(loc_config, global_config, attempt + 1)
        else:
            raise RuntimeError(f"🚨 [{loc_config['name']}] 连续 {max_attempts} 次抓取失败：{e}")


def scrape(attempt: int = 1) -> list[dict]:
    config = load_config()
    location_configs = get_locations(config)

    all_jobs: list[dict] = []
    errors: list[str] = []

    for loc_config in location_configs:
        try:
            jobs = _scrape_one_location(loc_config, config, attempt)
            all_jobs.extend(jobs)
        except RuntimeError as e:
            errors.append(str(e))
            logger.error(str(e))

    if errors and not all_jobs:
        raise RuntimeError("\n".join(errors))

    logger.info(f"全部地区抓取完成，合计 {len(all_jobs)} 条")
    return all_jobs
