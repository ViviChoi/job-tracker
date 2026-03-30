"""
HR Finder — 多引擎搜索目标公司的 LinkedIn HR/招聘人员
引擎：Bing / DuckDuckGo / Brave / Yahoo / Yandex / SearXNG
按公司名缓存结果（默认 6 小时），避免重复触发限流。
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).parent / "data" / "hr_cache"
_CACHE_TTL_HOURS = 6

_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]

_LI_SLUG_RE = re.compile(r'linkedin\.com/in/([\w\-]+)')

# Public SearXNG instances (try in order)
_SEARXNG_INSTANCES = [
    "https://searx.be",
    "https://search.inetol.net",
    "https://opnxng.com",
]


def _headers() -> dict:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


def _build_query(company: str) -> str:
    # "at Company Name" matches LinkedIn's current-position title format:
    # "First Last - Title at Company | LinkedIn"
    # This is far more reliable than searching company name alone, which matches
    # past jobs, mentions, followed companies, etc.
    return f'site:linkedin.com/in/ "at {company}"'


# ── Cache ─────────────────────────────────────────────────────

def _cache_path(company: str) -> Path:
    safe = re.sub(r'[^\w\- ]', '', company).strip().replace(" ", "_")[:60]
    return _CACHE_DIR / f"{safe}.json"


def _load_cache(company: str) -> list[dict] | None:
    path = _cache_path(company)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        cached_at = datetime.fromisoformat(data["cached_at"])
        if datetime.now() - cached_at < timedelta(hours=_CACHE_TTL_HOURS):
            logger.info(f"[{company}] 使用缓存结果（{len(data['contacts'])} 个联系人）")
            return data["contacts"]
    except Exception:
        pass
    return None


def _save_cache(company: str, contacts: list[dict]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(company).write_text(json.dumps({
        "cached_at": datetime.now().isoformat(),
        "contacts": contacts,
    }, ensure_ascii=False, indent=2))


# ── Parsers ───────────────────────────────────────────────────

def _parse_name_title(text: str) -> tuple[str, str]:
    text = re.sub(r'\s*\|.*$', '', text).strip()
    for sep in [' - ', ' – ', ' · ']:
        if sep in text:
            parts = text.split(sep, 1)
            name = parts[0].strip()
            title = parts[1].strip() if len(parts) > 1 else ""
            if name and len(name) < 50 and not name.islower():
                return name, title
    return "", ""


def _name_from_slug(slug: str) -> str:
    parts = slug.split("-")
    clean = [p for p in parts if not (len(p) > 6 and re.search(r'\d', p) and re.search(r'[a-z]', p))]
    return " ".join(p.capitalize() for p in (clean or parts)[:3])


def _extract_from_anchors(soup: BeautifulSoup, seen: set) -> list[dict]:
    results = []
    for a in soup.find_all("a", href=True):
        m = _LI_SLUG_RE.search(a["href"])
        if not m:
            continue
        slug = m.group(1)
        if slug in seen:
            continue
        seen.add(slug)
        name, title = _parse_name_title(a.get_text(strip=True))
        results.append({
            "linkedin_url": f"https://www.linkedin.com/in/{slug}",
            "name": name,
            "title": title,
            "snippet": "",
        })
    return results


# ── Search engines ────────────────────────────────────────────

def _search_bing(query: str) -> list[dict]:
    try:
        resp = requests.get(
            "https://www.bing.com/search",
            params={"q": query, "count": 20},
            headers=_headers(), timeout=15,
        )
        if not resp.ok:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        results, seen = [], set()
        for a in soup.select("li.b_algo h2 a"):
            m = _LI_SLUG_RE.search(a.get("href", ""))
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            name, title = _parse_name_title(a.get_text(strip=True))
            li = a.find_parent("li")
            snippet = ""
            if li:
                p = li.select_one(".b_caption p")
                snippet = p.get_text(strip=True) if p else ""
            results.append({"linkedin_url": f"https://www.linkedin.com/in/{m.group(1)}", "name": name, "title": title, "snippet": snippet})
        return results
    except Exception as e:
        logger.warning(f"Bing: {e}")
        return []


def _search_duckduckgo(query: str) -> list[dict]:
    try:
        resp = requests.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": query},
            headers=_headers(), timeout=15,
        )
        if not resp.ok or len(resp.text) < 500 or "captcha" in resp.text.lower():
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        seen = set()
        return _extract_from_anchors(soup, seen)
    except Exception as e:
        logger.warning(f"DuckDuckGo: {e}")
        return []


def _search_brave(query: str) -> list[dict]:
    try:
        resp = requests.get(
            "https://search.brave.com/search",
            params={"q": query, "source": "web"},
            headers=_headers(), timeout=15,
        )
        if not resp.ok or resp.status_code == 429:
            return []
        slugs = _LI_SLUG_RE.findall(resp.text)
        seen, results = set(), []
        for slug in slugs:
            if slug not in seen:
                seen.add(slug)
                results.append({"linkedin_url": f"https://www.linkedin.com/in/{slug}", "name": "", "title": "", "snippet": ""})
        return results
    except Exception as e:
        logger.warning(f"Brave: {e}")
        return []


def _search_yahoo(query: str) -> list[dict]:
    try:
        resp = requests.get(
            "https://search.yahoo.com/search",
            params={"p": query, "n": 20},
            headers=_headers(), timeout=15,
        )
        if not resp.ok:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        seen = set()
        return _extract_from_anchors(soup, seen)
    except Exception as e:
        logger.warning(f"Yahoo: {e}")
        return []


def _search_yandex(query: str) -> list[dict]:
    try:
        resp = requests.get(
            "https://yandex.com/search/",
            params={"text": query},
            headers=_headers(), timeout=15,
        )
        if not resp.ok:
            return []
        slugs = _LI_SLUG_RE.findall(resp.text)
        seen, results = set(), []
        for slug in slugs:
            if slug not in seen:
                seen.add(slug)
                results.append({"linkedin_url": f"https://www.linkedin.com/in/{slug}", "name": "", "title": "", "snippet": ""})
        return results
    except Exception as e:
        logger.warning(f"Yandex: {e}")
        return []


def _search_searxng(query: str) -> list[dict]:
    for instance in _SEARXNG_INSTANCES:
        try:
            resp = requests.get(
                f"{instance}/search",
                params={"q": query, "format": "json", "engines": "google,bing,duckduckgo"},
                headers=_headers(), timeout=15,
            )
            if not resp.ok:
                continue
            data = resp.json()
            results, seen = [], set()
            for r in data.get("results", []):
                url = r.get("url", "")
                m = _LI_SLUG_RE.search(url)
                if not m or m.group(1) in seen:
                    continue
                seen.add(m.group(1))
                name, title = _parse_name_title(r.get("title", ""))
                results.append({
                    "linkedin_url": f"https://www.linkedin.com/in/{m.group(1)}",
                    "name": name,
                    "title": title,
                    "snippet": r.get("content", ""),
                })
            if results:
                logger.info(f"SearXNG ({instance}) 返回 {len(results)} 条")
                return results
        except Exception as e:
            logger.warning(f"SearXNG {instance}: {e}")
    return []


# ── Main ──────────────────────────────────────────────────────

def find_hr_contacts(company: str, max_results: int = 5) -> list[dict]:
    cached = _load_cache(company)
    if cached is not None:
        return cached

    query = _build_query(company)
    logger.info(f"多引擎搜索 [{company}] HR 联系人…")

    # Only use engines that extract page titles — title is required to verify
    # current employment ("Name - Title at Company | LinkedIn").
    # Brave and Yandex only extract raw URLs without titles, making it
    # impossible to confirm current vs. past employment; excluded.
    engines = [_search_bing, _search_duckduckgo, _search_yahoo, _search_searxng]

    seen_slugs: set[str] = set()
    all_results: list[dict] = []

    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(fn, query): fn.__name__ for fn in engines}
        for future in as_completed(futures):
            try:
                for contact in future.result():
                    slug = contact["linkedin_url"].split("/in/")[-1].strip("/")
                    if slug not in seen_slugs:
                        seen_slugs.add(slug)
                        all_results.append(contact)
            except Exception as e:
                logger.warning(f"结果处理失败：{e}")

    # Only criterion: confirmed current employee ("at Company" in page title)
    contacts = [c for c in all_results if _is_current_employee(c, company)]

    if not contacts:
        logger.warning(f"[{company}] 未能确认任何当前在职员工，不推送联系人")

    contacts = contacts[:max_results]

    for c in contacts:
        if not c["name"]:
            slug = c["linkedin_url"].split("/in/")[-1].strip("/")
            c["name"] = _name_from_slug(slug)

    _save_cache(company, contacts)
    logger.info(f"找到 {len(contacts)} 个 [{company}] 在职员工（共 {len(all_results)} 条去重后）")
    return contacts


def _is_current_employee(contact: dict, company: str) -> bool:
    """
    Verify the contact currently works at the company.

    LinkedIn page titles follow a fixed format:
        "Name - Current Title at Current Company | LinkedIn"
    This reflects the CURRENT position only.

    Rules:
    1. Check ONLY the title — snippets contain past-job text and cause false positives.
    2. Contacts with no title (Brave/Yandex only extract URLs) are excluded.
    3. Use a negative lookahead so "at Google" does NOT match "at Google DeepMind":
       after the company name there must NOT be whitespace followed by a letter,
       which would indicate the company name continues (e.g. "DeepMind", "Wholesale").
    """
    company_lower = company.lower().strip()
    title = contact.get("title", "").lower()

    if not title:
        return False

    # "at Company" must be followed by end-of-content, not more company-name words.
    # (?!\s+[a-z]) — negative lookahead: fail if whitespace + letter follows,
    # e.g. "at google deepmind" → "at google" rejected because " d" follows.
    pattern = re.escape(f"at {company_lower}") + r"(?!\s+[a-z])"
    return bool(re.search(pattern, title))
