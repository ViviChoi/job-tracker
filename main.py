from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from datetime import datetime

from scraper import scrape
from matcher import should_push
from storage import init_db, is_seen, save_job, get_pending_jobs, mark_notified, job_id
from notifier import (
    notify_job, notify_batch, notify_warning, notify_error, notify_recovered
)
from scheduler import load_config, get_interval_seconds, should_notify_now, is_quiet_hours
from telegram_poller import start_polling
from outreach_gen import start_api_server as start_outreach_api
from web_ui import start_web_ui

_DATA_DIR = Path(__file__).parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_DATA_DIR / "job-tracker.log"),
    ],
)
logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 86400  # 每24小时发一次心跳
_last_heartbeat = 0
_consecutive_failures = 0
_paused_until = 0


def run_cycle() -> None:
    global _consecutive_failures, _paused_until, _last_heartbeat

    config = load_config()

    # Check if paused due to consecutive failures
    if _paused_until > time.time():
        remaining = int((_paused_until - time.time()) / 60)
        logger.info(f"暂停中，{remaining} 分钟后恢复")
        return

    # Heartbeat
    now = time.time()
    if now - _last_heartbeat > HEARTBEAT_INTERVAL:
        from notifier import _dispatch
        _dispatch("💚 Job Tracker 运行正常", config, subject="心跳确认")
        _last_heartbeat = now

    # Scrape
    try:
        jobs = scrape()
        if _consecutive_failures > 0:
            _consecutive_failures = 0
            notify_recovered()
    except RuntimeError as e:
        _consecutive_failures += 1
        error_msg = str(e)
        logger.error(error_msg)

        pause_minutes = config["search"]["retry"].get("pause_on_failure_minutes", 60)
        _paused_until = time.time() + pause_minutes * 60
        notify_error(f"{error_msg}\n已暂停 {pause_minutes} 分钟")
        return

    # Filter seen + match
    new_matched = []
    for job in jobs:
        if is_seen(job["link"]):
            continue

        try:
            push, score, reason = should_push(job)
        except Exception as e:
            logger.error(f"匹配失败（{job.get('title','')}）：{e}")
            notify_error(f"匹配模块异常：{e}")
            continue

        # Always save to DB (for dedup); non-matching jobs marked notified=1 so they skip batch send
        save_job(job, score, reason, notified=0 if push else 1)

        if push:
            new_matched.append({"job": job, "score": score, "reason": reason, "mode": config["matching"]["mode"]})

    if not new_matched:
        logger.info("本轮无新匹配职位")
        return

    logger.info(f"本轮新匹配 {len(new_matched)} 条")

    # Dispatch based on mode
    notification_mode = config["notification"].get("mode", "realtime")

    if notification_mode == "realtime":
        if is_quiet_hours(config):
            logger.info("免打扰时段，暂存职位等待推送")
            return

        # Flush jobs held during previous quiet hours
        pending = get_pending_jobs()
        for r in pending:
            jid = r["id"]
            if jid in [job_id(i["job"]["link"]) for i in new_matched]:
                continue  # will be sent below
            try:
                notify_job(
                    {"title": r["title"], "company": r["company"],
                     "location": r["location"], "link": r["link"]},
                    r["match_score"], r["match_reason"],
                )
                mark_notified(jid)
            except Exception as e:
                logger.error(f"积压职位推送失败：{e}")

        for item in new_matched:
            try:
                notify_job(item["job"], item["score"], item["reason"])
                mark_notified(job_id(item["job"]["link"]))
            except Exception as e:
                logger.error(f"推送失败：{e}")

    else:
        # Scheduled mode: jobs stay pending, batch send at scheduled times
        if should_notify_now(config):
            pending = get_pending_jobs()
            if pending:
                batch = [
                    {
                        "job": {"title": r["title"], "company": r["company"],
                                "location": r["location"], "link": r["link"]},
                        "score": r["match_score"],
                        "reason": r["match_reason"],
                        "mode": config["matching"]["mode"],
                    }
                    for r in pending
                ]
                try:
                    notify_batch(batch)
                    for r in pending:
                        mark_notified(r["id"])
                except Exception as e:
                    logger.error(f"批量推送失败：{e}")


def check_interval_warning(config: dict) -> None:
    interval = config["search"].get("interval_minutes", 60)
    if interval < 30:
        notify_warning(
            f"当前抓取间隔为 {interval} 分钟，频率过高可能导致 IP 被封或账号风险，建议设置为 30 分钟以上"
        )


def check_resume(config: dict) -> None:
    """检查简历文件是否存在，关键词模式下缺失则报错退出。"""
    mode = config.get("matching", {}).get("mode", "keywords")
    resume_file = config.get("matching", {}).get("resume_file", "resume.txt")
    resume_path = Path(__file__).parent / resume_file

    if not resume_path.exists():
        if mode == "keywords":
            msg = (
                "❌ 简历文件未找到\n\n"
                f"当前匹配模式：关键词模式\n"
                f"缺少文件：{resume_file}\n\n"
                "请前往配置页面（http://localhost:8080）→「简历上传」上传简历后重新启动"
            )
            logger.error(msg)
            try:
                notify_warning(msg)
            except Exception:
                pass
            raise SystemExit(1)
        else:
            logger.warning(
                f"简历文件 {resume_file} 不存在，当前为 AI 模式无需简历，继续运行"
            )


def main() -> None:
    logger.info("Job Tracker 启动")
    init_db()
    start_polling()          # 启动 Telegram 回调监听（后台线程）
    config_boot = load_config()
    outreach_port = config_boot.get("outreach", {}).get("api_port", 8082)
    start_outreach_api(port=outreach_port)  # 启动 Outreach API（后台线程）
    web_ui_port = config_boot.get("web_ui", {}).get("port", 8083)
    start_web_ui(port=web_ui_port)          # 启动 Web UI（后台线程）

    config = load_config()
    check_resume(config)      # 检查简历，关键词模式缺失则退出
    check_interval_warning(config)

    interval = get_interval_seconds(config)
    logger.info(f"抓取间隔：{interval // 60} 分钟")

    while True:
        try:
            run_cycle()
        except Exception as e:
            logger.error(f"主循环异常：{e}")

        # Reload config each cycle to pick up changes
        config = load_config()
        interval = get_interval_seconds(config)
        time.sleep(interval)


if __name__ == "__main__":
    main()
