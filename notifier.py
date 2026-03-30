from __future__ import annotations

import json
import logging
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    return json.loads(config_path.read_text())


def get_timezone(config: dict) -> ZoneInfo:
    tz_config = config["notification"].get("timezone", "auto")
    if tz_config == "auto":
        try:
            res = requests.get("https://ipapi.co/json/", timeout=10)
            tz_str = res.json().get("timezone", "UTC")
            return ZoneInfo(tz_str)
        except Exception:
            return ZoneInfo("UTC")
    return ZoneInfo(tz_config)


def is_quiet_hours(config: dict) -> bool:
    quiet = config["notification"].get("quiet_hours", {})
    if not quiet.get("enabled", False):
        return False

    tz = get_timezone(config)
    now = datetime.now(tz).time()

    start_str = quiet.get("start", "23:00")
    end_str = quiet.get("end", "07:00")
    start = dtime(*map(int, start_str.split(":")))
    end = dtime(*map(int, end_str.split(":")))

    if start <= end:
        return start <= now <= end
    # Overnight: e.g. 23:00 - 07:00
    return now >= start or now <= end


def format_job_html(job: dict, score: int, reason: str, mode: str) -> str:
    score_line = f"⭐ 匹配度：{score}分" if mode == "keywords" else f"✅ {reason}"
    posted = job.get("posted_at", "")
    posted_line = f"🕐 {posted}" if posted else ""

    lines = [
        "🆕 <b>新职位匹配</b>",
        "",
        f"<b>{job.get('title', '')}</b>",
        f"🏢 {job.get('company', '')}",
        f"📍 {job.get('location', '')}",
    ]
    if posted_line:
        lines.append(posted_line)
    lines.append(score_line)
    lines.append("")
    lines.append(f'<a href="{job.get("link", "")}">查看职位</a>')
    return "\n".join(lines)


def format_batch_html(jobs: list[dict]) -> str:
    lines = [f"📋 <b>职位推送（{len(jobs)}条）</b>", ""]
    for i, item in enumerate(jobs, 1):
        job = item["job"]
        score = item["score"]
        reason = item["reason"]
        mode = item["mode"]
        score_line = f"匹配度 {score}分" if mode == "keywords" else reason
        lines.append(
            f"{i}. <b>{job.get('title','')}</b> — {job.get('company','')}\n"
            f"   📍 {job.get('location','')} | {score_line}\n"
            f'   <a href="{job.get("link","")}">查看</a>'
        )
        lines.append("")
    return "\n".join(lines)


def send_telegram(text: str, config: dict, reply_markup: Optional[dict] = None) -> None:
    tg = config["notification"]["channels"]["telegram"]
    if not tg.get("enabled", False):
        return
    token = tg.get("bot_token", "")
    chat_id = tg.get("chat_id", "")
    if not token or not chat_id:
        raise ValueError("Telegram bot_token 或 chat_id 未配置")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    res = requests.post(url, json=payload, timeout=15)
    if not res.ok:
        raise RuntimeError(f"Telegram 推送失败：{res.status_code} {res.text[:200]}")


def send_email(subject: str, body: str, config: dict) -> None:
    em = config["notification"]["channels"]["email"]
    if not em.get("enabled", False):
        return

    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = em["username"]
    msg["To"] = em["to"]

    with smtplib.SMTP(em["smtp_host"], em["smtp_port"]) as s:
        s.starttls()
        s.login(em["username"], em["password"])
        s.send_message(msg)


def save_pending_job(job: dict) -> str:
    """保存职位数据供 Telegram 回调生成简历，返回 job_id。"""
    import hashlib
    jid = hashlib.md5(job.get("link", "").encode()).hexdigest()[:12]
    pending_dir = Path(__file__).parent / "data" / "pending_resume"
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / f"{jid}.json").write_text(
        json.dumps(job, ensure_ascii=False, indent=2)
    )
    return jid


def notify_job(job: dict, score: int, reason: str) -> None:
    config = load_config()
    mode = config["matching"]["mode"]
    text = format_job_html(job, score, reason, mode)

    # Add inline buttons if Telegram is enabled and job has a link
    reply_markup = None
    if (config["notification"]["channels"]["telegram"].get("enabled")
            and job.get("link")):
        jid = save_pending_job(job)
        reply_markup = {
            "inline_keyboard": [[
                {"text": "✅ 感兴趣", "callback_data": f"interested:{jid}"},
                {"text": "👎 减少推荐", "callback_data": f"dislike:{jid}"},
            ]]
        }

    tg = config["notification"]["channels"]["telegram"]
    if tg.get("enabled"):
        try:
            send_telegram(text, config, reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Telegram 推送失败：{e}")
    try:
        send_email(f"新职位：{job.get('title','')}", text.replace("\n", "<br>"), config)
    except Exception as e:
        logger.error(f"Email 推送失败：{e}")

    _on_job_matched(job, config)


def _on_job_matched(job: dict, config: dict) -> None:
    """简历生成器接入点：每次匹配到职位后实时触发，不受定时推送影响。"""
    rg = config.get("resume_generator", {})
    if not rg.get("enabled", False):
        return

    payload = {
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "link": job.get("link", ""),
        "description": job.get("description", ""),
        "posted_at": job.get("posted_at", ""),
    }

    output = rg.get("output", {})
    mode = output.get("mode", "file")

    try:
        if mode == "file":
            default_path = str(Path(__file__).parent / "data" / "matched_jobs")
            _output_to_file(payload, output.get("file_path") or default_path)
        elif mode == "webhook":
            _output_to_webhook(payload, output.get("webhook_url", ""))
    except Exception as e:
        logger.error(f"简历生成器输出失败（{mode}）：{e}")


def _output_to_file(payload: dict, file_path: str) -> None:
    import json as _json
    p = Path(file_path)
    dir_path = (Path(__file__).parent / p) if not p.is_absolute() and not file_path.startswith("~") else p.expanduser()
    dir_path.mkdir(parents=True, exist_ok=True)
    safe_company = "".join(c for c in payload["company"] if c.isalnum() or c in " _-")[:30]
    safe_title = "".join(c for c in payload["title"] if c.isalnum() or c in " _-")[:40]
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_company}_{safe_title}.json"
    out_file = dir_path / filename
    out_file.write_text(_json.dumps(payload, ensure_ascii=False, indent=2))
    logger.info(f"简历生成器输出：{out_file}")


def _output_to_webhook(payload: dict, url: str) -> None:
    if not url:
        raise ValueError("webhook_url 未配置")
    resp = requests.post(url, json=payload, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Webhook 返回 {resp.status_code}: {resp.text[:200]}")


def notify_batch(jobs: list[dict]) -> None:
    if not jobs:
        return
    config = load_config()
    text = format_batch_html(jobs)
    _dispatch(text, config, subject=f"职位推送（{len(jobs)}条）")


def notify_warning(message: str) -> None:
    config = load_config()
    _dispatch(f"⚠️ {message}", config, subject="Job Tracker 告警")


def notify_error(message: str) -> None:
    config = load_config()
    _dispatch(f"🚨 {message}", config, subject="Job Tracker 错误")


def notify_recovered() -> None:
    config = load_config()
    _dispatch("✅ 抓取已恢复正常", config, subject="Job Tracker 恢复")


def notify_storage_switched(new_mode: str) -> None:
    config = load_config()
    _dispatch(
        f"⚠️ 存储方式已切换至 <b>{new_mode}</b>，历史去重记录不会迁移，近期可能出现少量重复推送",
        config,
        subject="存储方式已切换",
    )


def _dispatch(text: str, config: dict, subject: str = "") -> None:
    try:
        send_telegram(text, config)
    except Exception as e:
        logger.error(f"Telegram 推送失败：{e}")

    try:
        send_email(subject, text.replace("\n", "<br>"), config)
    except Exception as e:
        logger.error(f"Email 推送失败：{e}")
