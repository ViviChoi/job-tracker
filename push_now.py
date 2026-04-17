"""
手动触发推送 — 只推 hours_old 时效内的待推送职位（默认 24h）。
用法：python push_now.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from storage import get_pending_jobs, mark_notified, load_config
from notifier import notify_job


def main() -> None:
    config = load_config()
    hours_old: int = config.get("search", {}).get("hours_old", 24)
    now = datetime.now(timezone.utc)

    pending = get_pending_jobs()
    fresh, expired = [], []

    for r in pending:
        # Starred / user-interest jobs: always keep regardless of age
        if r.get("starred") or r.get("status", "new") != "new":
            fresh.append(r)
            continue
        try:
            created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_hours = (now - created).total_seconds() / 3600
            if age_hours <= hours_old:
                fresh.append(r)
            else:
                expired.append(r)
        except Exception:
            fresh.append(r)

    print(f"待推送：{len(pending)} 条  |  时效内（≤{hours_old}h）：{len(fresh)} 条  |  已过期：{len(expired)} 条")

    if expired:
        for r in expired:
            mark_notified(r["id"])
        print(f"已将 {len(expired)} 条过期职位标记为已通知（不推送）")

    if not fresh:
        print("无需推送。")
        return

    sent = errors = 0
    for r in fresh:
        try:
            notify_job(
                {"title": r["title"], "company": r["company"],
                 "location": r["location"], "link": r.get("link", "")},
                r["match_score"], r["match_reason"],
            )
            mark_notified(r["id"])
            sent += 1
        except Exception as e:
            print(f"  推送失败：{r['title']} — {e}")
            errors += 1

    print(f"完成：推送 {sent} 条，失败 {errors} 条")


if __name__ == "__main__":
    main()
