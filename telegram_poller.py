"""
Telegram Bot 回调监听器

按钮交互流程：
  职位卡 [✅ 感兴趣]
    → 并行：生成简历 + 搜索 HR 联系人
    → 编辑为结果卡（ATS 摘要 + HR 联系人按钮）
    → 简历文件单独发送至简历频道

  结果卡 [✉️ 联系人姓名]
    → 原地编辑为联系消息卡（Opus 生成）+ [← 返回] 按钮

  联系消息卡 [← 返回]
    → 原地恢复为结果卡
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Thread

import requests

logger = logging.getLogger(__name__)

_RESUME_GEN_URL = "http://localhost:8081"
_PENDING_DIR = Path(__file__).parent / "data" / "pending_resume"
_PENDING_CONTACT_DIR = Path(__file__).parent / "data" / "pending_contact"


def load_config() -> dict:
    return json.loads((Path(__file__).parent / "config.json").read_text())


# ── Telegram API helpers ──────────────────────────────────────

def _tg_post(token: str, method: str, **kwargs) -> dict:
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=kwargs,
            timeout=35,
        )
        return res.json()
    except Exception as e:
        logger.error(f"Telegram API [{method}] 请求失败：{e}")
        return {"ok": False, "description": str(e)}


def _answer_callback(token: str, callback_query_id: str, text: str = "") -> None:
    _tg_post(token, "answerCallbackQuery",
             callback_query_id=callback_query_id, text=text, show_alert=False)


def _send_message(token: str, chat_id: str, text: str) -> dict:
    return _tg_post(token, "sendMessage",
                    chat_id=chat_id, text=text,
                    parse_mode="HTML", disable_web_page_preview=True)


def _edit_message(token: str, chat_id: str, message_id: int,
                  text: str, keyboard: list | None = None) -> None:
    markup = {"inline_keyboard": keyboard or []}
    _tg_post(token, "editMessageText",
             chat_id=chat_id, message_id=message_id,
             text=text, parse_mode="HTML",
             disable_web_page_preview=True,
             reply_markup=markup)


def _edit_keyboard(token: str, chat_id: str, message_id: int,
                   keyboard: list | None = None) -> None:
    _tg_post(token, "editMessageReplyMarkup",
             chat_id=chat_id, message_id=message_id,
             reply_markup={"inline_keyboard": keyboard or []})


def _send_document(token: str, chat_id: str, filename: str,
                   content: str, caption: str = "") -> None:
    requests.post(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
        files={"document": (filename, content.encode("utf-8"), "text/markdown")},
        timeout=30,
    )


# ── File naming & resume delivery ────────────────────────────

def _safe_part(text: str, max_len: int) -> str:
    cleaned = "".join(c for c in text if c.isalnum() or c in " _-").strip()
    return cleaned[:max_len].strip().replace(" ", "_")


def _send_resume_file(main_token: str, main_chat_id: str,
                      company: str, title: str, resume_md: str) -> None:
    """发送简历文件，若配置了独立 resume_bot 则发到那里。"""
    filename = f"resume_{_safe_part(company, 25)}_{_safe_part(title, 35)}.md"

    config = load_config()
    rb = config.get("resume_bot", {})
    if rb.get("enabled") and rb.get("bot_token") and rb.get("chat_id"):
        dest_token, dest_chat = rb["bot_token"], str(rb["chat_id"])
    else:
        dest_token, dest_chat = main_token, main_chat_id

    caption = f"📄 <b>{title}</b>\n🏢 {company}"
    _send_document(dest_token, dest_chat, filename, resume_md, caption=caption)
    logger.info(f"简历文件已发送：{filename}")


# ── Pending data helpers ──────────────────────────────────────

def _load_pending_job(jid: str) -> dict | None:
    path = _PENDING_DIR / f"{jid}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _compute_contact_id(contact: dict, job: dict) -> str:
    import hashlib
    return hashlib.md5(
        (contact.get("linkedin_url", "") + job.get("link", "")).encode()
    ).hexdigest()[:12]


def _save_pending_contact(contact: dict, job: dict,
                          parent_text: str, parent_keyboard: list) -> str:
    """保存联系人数据及父卡片状态（用于返回），返回 contact_id。"""
    cid = _compute_contact_id(contact, job)
    _PENDING_CONTACT_DIR.mkdir(parents=True, exist_ok=True)
    (_PENDING_CONTACT_DIR / f"{cid}.json").write_text(
        json.dumps({
            "contact": contact,
            "job": job,
            "parent_text": parent_text,
            "parent_keyboard": parent_keyboard,
        }, ensure_ascii=False, indent=2)
    )
    return cid


def _load_pending_contact(cid: str) -> dict | None:
    path = _PENDING_CONTACT_DIR / f"{cid}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


# ── Resume generation ─────────────────────────────────────────

def _fetch_jd(link: str) -> str:
    """实时从职位链接抓取最新 JD 正文。LinkedIn 用 jobspy 抓。"""
    import re
    m = re.search(r"linkedin\.com/jobs/view/(\d+)", link)
    if m:
        try:
            from jobspy import scrape_jobs
            df = scrape_jobs(
                site_name=["linkedin"],
                search_term="",
                linkedin_company_ids=None,
                linkedin_fetch_description=True,
                results_wanted=1,
                offset=0,
            )
            # jobspy 不支持按 job_id 直查，改用直接请求 LinkedIn jobs API
            raise NotImplementedError
        except Exception:
            pass
        # 直接请求 LinkedIn 非登录版 job detail API
        job_id = m.group(1)
        try:
            resp = requests.get(
                f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}",
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                timeout=15,
            )
            if resp.ok:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(resp.text, "html.parser")
                el = soup.select_one(".description__text") or soup.select_one("section.show-more-less-html")
                if el:
                    return el.get_text(separator="\n", strip=True)
        except Exception:
            pass

    # 兜底：走 Resume Generator 的通用抓取
    try:
        res = requests.post(
            f"{_RESUME_GEN_URL}/api/fetch_jd",
            json={"url": link},
            timeout=30,
        )
        data = res.json()
        return data.get("jd", "").strip() if data.get("success") else ""
    except Exception:
        return ""


def _generate_resume(job: dict) -> dict:
    """调用 Resume Generator API 生成简历。按按钮时实时抓取最新 JD。"""
    link = job.get("link", "")
    description = _fetch_jd(link) if link else ""
    if not description:
        description = job.get("description", "").strip()  # 兜底用抓取时存的
    if not description:
        return {"success": False, "error": "无法获取职位 JD，请检查链接是否有效"}

    job_info = {k: job.get(k, "") for k in ("title", "company", "location", "link", "posted_at")}
    try:
        res = requests.post(
            f"{_RESUME_GEN_URL}/api/generate",
            json={"jd": description, "job_info": job_info},
            timeout=180,
        )
        return res.json()
    except requests.exceptions.ConnectionError:
        return {
            "success": False,
            "error": "Resume Generator 未运行，请先启动（双击 Double Click to Start_Mac.command）",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Card text builders ────────────────────────────────────────

def _build_result_card(job: dict, resume_result: dict, contacts: list[dict]) -> str:
    """构建感兴趣后的结果卡文字（简历摘要 + HR 区域标题）。"""
    title = job.get("title", "")
    company = job.get("company", "")
    location = job.get("location", "")
    link = job.get("link", "")
    posted_at = job.get("posted_at", "")

    # Header
    lines = [
        f"✅  <b>{title}</b>  @  {company}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    if location:
        lines.append(f"📍 {location}" + (f"   🕐 {posted_at}" if posted_at else ""))
    if link:
        lines.append(f'🔗 <a href="{link}">查看职位</a>')

    # Resume ATS section
    lines.append("")
    if resume_result.get("success"):
        ats = resume_result.get("ats", {})
        score = ats.get("total_score", "—")
        matched = "  ·  ".join(ats.get("matched", [])[:5]) or "—"
        missing = "  ·  ".join(ats.get("missing", [])[:5]) or "—"
        cost = resume_result.get("cost_usd", 0)
        lines += [
            f"📊  ATS 评分  <b>{score} / 100</b>",
            f"✔  {matched}",
            f"✘  {missing}",
            f"💰  简历费用 ${cost:.4f}   📄 已发至简历频道",
        ]
    else:
        err = resume_result.get("error", "未知错误")
        lines.append(f"❌  简历生成失败：{err}")

    # HR section title
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    if contacts:
        lines.append(f"👥  {company} 在职员工（{len(contacts)} 位）")
        for c in contacts:
            name = c.get("name") or "—"
            role = c.get("title") or ""
            li_url = c.get("linkedin_url", "")
            entry = f"  · <b>{name}</b>"
            if role:
                entry += f"  {role}"
            if li_url:
                entry += f'  <a href="{li_url}">↗</a>'
            lines.append(entry)
    else:
        lines.append(f"🔍  未找到 {company} 的在职员工")

    return "\n".join(lines)


def _build_hr_keyboard(contacts: list[dict], job: dict,
                       parent_text: str) -> list[list[dict]]:
    """
    先算出所有 cid 和 keyboard，再统一保存（避免保存时 keyboard 为空的竞态）。
    """
    # Pass 1: compute cids + build keyboard without saving yet
    rows: list[tuple[str, dict]] = []
    keyboard: list[list[dict]] = []
    for contact in contacts:
        name = contact.get("name") or "联系人"
        title = contact.get("title", "")
        label = f"✉️  {name}"
        if title:
            short_title = title[:20] + ("…" if len(title) > 20 else "")
            label += f"  ·  {short_title}"
        cid = _compute_contact_id(contact, job)
        keyboard.append([{"text": label, "callback_data": f"contact:{cid}"}])
        rows.append((cid, contact))

    # Pass 2: save all contacts with the complete keyboard
    _PENDING_CONTACT_DIR.mkdir(parents=True, exist_ok=True)
    for cid, contact in rows:
        (_PENDING_CONTACT_DIR / f"{cid}.json").write_text(
            json.dumps({
                "contact": contact,
                "job": job,
                "parent_text": parent_text,
                "parent_keyboard": keyboard,
            }, ensure_ascii=False, indent=2)
        )

    return keyboard


# ── Callback handlers ─────────────────────────────────────────

def _handle_interested_callback(token: str, chat_id: str, callback_query_id: str,
                                 message_id: int, jid: str) -> None:
    """[✅ 感兴趣] → 并行生成简历 + 搜索 HR，完成后编辑为结果卡。"""
    _answer_callback(token, callback_query_id, "处理中…")
    _edit_keyboard(token, chat_id, message_id)  # 移除感兴趣按钮，防止重复点击

    job = _load_pending_job(jid)
    if not job:
        _send_message(token, chat_id, "⚠️ 职位信息已过期，请重新触发搜索")
        return

    company = job.get("company", "")
    title = job.get("title", "")

    # 发一条占位消息，之后原地编辑
    resp = _send_message(token, chat_id,
                         f"⏳  <b>{title} @ {company}</b>\n\n"
                         f"正在生成简历并搜索招聘联系人…")
    status_msg_id = resp.get("result", {}).get("message_id")

    from hr_finder import find_hr_contacts

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_resume = ex.submit(_generate_resume, job)
        f_hr = ex.submit(find_hr_contacts, company, 5)

    try:
        resume_result = f_resume.result()
    except Exception as e:
        logger.error(f"简历生成任务异常：{e}")
        resume_result = {"success": False, "error": str(e)}

    try:
        contacts = f_hr.result()
    except Exception as e:
        logger.error(f"HR 搜索任务异常：{e}")
        contacts = []

    # 构建结果卡（keyboard 先于保存完成，无竞态）
    card_text = _build_result_card(job, resume_result, contacts)
    keyboard = _build_hr_keyboard(contacts, job, card_text) if contacts else []

    # 将占位消息编辑为完整结果卡
    if status_msg_id:
        try:
            _edit_message(token, chat_id, status_msg_id, card_text, keyboard)
        except Exception as e:
            logger.warning(f"编辑结果卡失败，改为新发：{e}")
            _send_message(token, chat_id, card_text)
    else:
        _send_message(token, chat_id, card_text)

    # 简历文件单独发送
    if resume_result.get("success"):
        resume_md = resume_result.get("resume_markdown", "")
        _send_resume_file(token, chat_id, company, title, resume_md)
        try:
            from storage import update_job
            update_job(jid, {"status": "resume_generated"})
        except Exception:
            pass

    logger.info(f"感兴趣处理完成：{title} @ {company}  "
                f"ATS={resume_result.get('ats', {}).get('total_score', '—')}  "
                f"HR联系人={len(contacts)}")


def _handle_contact_callback(token: str, chat_id: str, callback_query_id: str,
                              message_id: int, cid: str) -> None:
    """[✉️ 联系人] → 原地编辑为 Opus 生成的联系消息卡 + [← 返回] 按钮。"""
    _answer_callback(token, callback_query_id, "生成中…")

    data = _load_pending_contact(cid)
    if not data:
        _send_message(token, chat_id, "⚠️ 联系人信息已过期，请重新触发搜索")
        return

    contact = data["contact"]
    job = data["job"]
    name = contact.get("name") or "该联系人"
    li_url = contact.get("linkedin_url", "")

    # 先编辑为等待状态
    _edit_message(token, chat_id, message_id,
                  f"⏳  正在为 <b>{name}</b> 生成个性化联系消息…\n通常需要 15–30 秒。")

    from outreach_gen import generate_outreach
    result = generate_outreach(contact, job)

    if not result.get("success"):
        err = result.get("error", "未知错误")
        _edit_message(token, chat_id, message_id,
                      f"❌  消息生成失败：{err}",
                      [[{"text": "← 返回", "callback_data": f"back:{cid}"}]])
        logger.warning(f"联系消息生成失败：{name}  err={err}")
        return

    msg_text = result["message"]
    model = result.get("model", "")
    cost = result.get("cost_usd", 0)
    in_tok = result.get("input_tokens", 0)
    out_tok = result.get("output_tokens", 0)

    card = (
        f"✉️  <b>{name}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{msg_text}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 {model}   📊 {in_tok}+{out_tok} tok   💰 ${cost:.5f}"
    )

    keyboard: list[list[dict]] = []
    if li_url:
        keyboard.append([{"text": "🔗 去 LinkedIn 发送", "url": li_url}])
    keyboard.append([{"text": "← 返回联系人列表", "callback_data": f"back:{cid}"}])

    _edit_message(token, chat_id, message_id, card, keyboard)
    logger.info(f"联系消息已生成：{name}  cost=${cost:.5f}  model={model}")


def _handle_back_callback(token: str, chat_id: str, callback_query_id: str,
                           message_id: int, cid: str) -> None:
    """[← 返回] → 将消息恢复为结果卡（含 HR 联系人按钮）。"""
    _answer_callback(token, callback_query_id)

    data = _load_pending_contact(cid)
    if not data:
        _edit_keyboard(token, chat_id, message_id)
        return

    parent_text = data.get("parent_text", "")
    parent_keyboard = data.get("parent_keyboard", [])
    _edit_message(token, chat_id, message_id, parent_text, parent_keyboard)


_DISLIKE_REASONS = [
    ("not_relevant",  "工作内容不相关"),
    ("wrong_location", "地点不符合"),
    ("citizenship",    "需要公民身份/签证"),
    ("too_senior",     "级别要求太高"),
    ("wrong_field",    "行业/职种方向不对"),
    ("other",          "其他原因"),
]


def _handle_dislike_callback(token: str, chat_id: str, callback_query_id: str,
                              message_id: int, jid: str) -> None:
    """[👎 减少推荐] → 展示原因选项。"""
    _answer_callback(token, callback_query_id, "请选择原因")

    keyboard = [
        [{"text": label, "callback_data": f"dislike_reason:{jid}:{code}"}]
        for code, label in _DISLIKE_REASONS
    ]
    keyboard.append([{"text": "✕ 取消", "callback_data": f"dislike_cancel:{jid}"}])

    _tg_post(token, "editMessageReplyMarkup",
             chat_id=chat_id, message_id=message_id,
             reply_markup={"inline_keyboard": keyboard})


def _handle_dislike_reason_callback(token: str, chat_id: str, callback_query_id: str,
                                     message_id: int, jid: str, reason_code: str) -> None:
    """[原因按钮] → 保存反馈，恢复原始按钮。"""
    reason_label = dict(_DISLIKE_REASONS).get(reason_code, reason_code)
    _answer_callback(token, callback_query_id, f"已记录：{reason_label}")

    try:
        from storage import save_feedback, update_job
        save_feedback(jid, reason_code)
        update_job(jid, {"status": "disliked"})
        logger.info(f"用户反馈已保存：job={jid} reason={reason_code}")
    except Exception as e:
        logger.error(f"保存反馈失败：{e}")

    # Restore original buttons after feedback
    _tg_post(token, "editMessageReplyMarkup",
             chat_id=chat_id, message_id=message_id,
             reply_markup={"inline_keyboard": [[
                 {"text": f"👎 已反馈：{reason_label}", "callback_data": "noop"},
             ]]})


def _handle_dislike_cancel_callback(token: str, chat_id: str, callback_query_id: str,
                                     message_id: int, jid: str) -> None:
    """[取消] → 恢复感兴趣+减少推荐按钮。"""
    _answer_callback(token, callback_query_id)
    _tg_post(token, "editMessageReplyMarkup",
             chat_id=chat_id, message_id=message_id,
             reply_markup={"inline_keyboard": [[
                 {"text": "✅ 感兴趣", "callback_data": f"interested:{jid}"},
                 {"text": "👎 减少推荐", "callback_data": f"dislike:{jid}"},
             ]]})


def _handle_gen_callback(token: str, chat_id: str, callback_query_id: str,
                          message_id: int, jid: str) -> None:
    """兼容旧版 gen: 按钮（仅生成简历，无 HR 搜索）。"""
    _answer_callback(token, callback_query_id, "正在生成简历，请稍候…")
    _edit_keyboard(token, chat_id, message_id)

    job = _load_pending_job(jid)
    if not job:
        _send_message(token, chat_id, "⚠️ 职位信息已过期，请重新触发搜索")
        return

    title = job.get("title", "")
    company = job.get("company", "")
    location = job.get("location", "")
    link = job.get("link", "")
    posted_at = job.get("posted_at", "")

    resp = _send_message(token, chat_id,
                         f"⏳  <b>{title} @ {company}</b>\n正在生成定制简历…")
    status_msg_id = resp.get("result", {}).get("message_id")

    try:
        from storage import update_job
        update_job(jid, {"status": "resume_generating"})
    except Exception:
        pass

    result = _generate_resume(job)

    if not result.get("success"):
        err = result.get("error", "未知错误")
        if status_msg_id:
            _edit_message(token, chat_id, status_msg_id, f"❌  简历生成失败：{err}")
        try:
            from storage import update_job
            update_job(jid, {"status": "resume_failed"})
        except Exception:
            pass
        logger.warning(f"简历生成失败：{title} @ {company}  err={err}")
        return

    ats = result.get("ats", {})
    score = ats.get("total_score", "—")
    matched = "  ·  ".join(ats.get("matched", [])[:5]) or "—"
    missing = "  ·  ".join(ats.get("missing", [])[:5]) or "—"
    cost = result.get("cost_usd", 0)
    posted_line = f"   🕐 {posted_at}" if posted_at else ""

    card = (
        f"✅  <b>{title}</b>  @  {company}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 {location}{posted_line}\n"
        f'🔗 <a href="{link}">查看职位</a>\n\n'
        f"📊  ATS 评分  <b>{score} / 100</b>\n"
        f"✔  {matched}\n"
        f"✘  {missing}\n"
        f"💰  ${cost:.4f}   📄 简历已发至简历频道"
    )

    if status_msg_id:
        try:
            _edit_message(token, chat_id, status_msg_id, card)
        except Exception:
            _send_message(token, chat_id, card)
    else:
        _send_message(token, chat_id, card)

    _send_resume_file(token, chat_id, company, title, result.get("resume_markdown", ""))

    try:
        from storage import update_job
        update_job(jid, {"status": "resume_generated"})
        (_PENDING_DIR / f"{jid}.json").unlink(missing_ok=True)
    except Exception:
        pass

    logger.info(f"简历生成完成（旧版回调）：{title} @ {company}  ATS={score}")


# ── Polling loop ──────────────────────────────────────────────

def _poll_loop(token: str, chat_id: str) -> None:
    offset = 0
    logger.info("Telegram 回调监听已启动")

    while True:
        try:
            res = _tg_post(token, "getUpdates",
                           offset=offset, timeout=30,
                           allowed_updates=["callback_query"])
            updates = res.get("result", [])

            for update in updates:
                offset = update["update_id"] + 1
                cq = update.get("callback_query")
                if not cq:
                    continue

                cb_data = cq.get("data", "")
                cq_id = cq["id"]
                msg = cq.get("message", {})
                msg_id = msg.get("message_id")
                sender_chat = str(msg.get("chat", {}).get("id", ""))

                if sender_chat != str(chat_id):
                    _answer_callback(token, cq_id, "无权限")
                    continue

                if cb_data.startswith("interested:"):
                    jid = cb_data[len("interested:"):]
                    Thread(target=_handle_interested_callback,
                           args=(token, chat_id, cq_id, msg_id, jid),
                           daemon=True).start()

                elif cb_data.startswith("contact:"):
                    cid = cb_data[len("contact:"):]
                    Thread(target=_handle_contact_callback,
                           args=(token, chat_id, cq_id, msg_id, cid),
                           daemon=True).start()

                elif cb_data.startswith("back:"):
                    cid = cb_data[len("back:"):]
                    Thread(target=_handle_back_callback,
                           args=(token, chat_id, cq_id, msg_id, cid),
                           daemon=True).start()

                elif cb_data.startswith("gen:"):
                    # 兼容旧版按钮
                    jid = cb_data[4:]
                    Thread(target=_handle_gen_callback,
                           args=(token, chat_id, cq_id, msg_id, jid),
                           daemon=True).start()

                elif cb_data.startswith("dislike:"):
                    jid = cb_data[len("dislike:"):]
                    Thread(target=_handle_dislike_callback,
                           args=(token, chat_id, cq_id, msg_id, jid),
                           daemon=True).start()

                elif cb_data.startswith("dislike_reason:"):
                    parts = cb_data[len("dislike_reason:"):].split(":", 1)
                    if len(parts) == 2:
                        jid, reason_code = parts
                        Thread(target=_handle_dislike_reason_callback,
                               args=(token, chat_id, cq_id, msg_id, jid, reason_code),
                               daemon=True).start()

                elif cb_data.startswith("dislike_cancel:"):
                    jid = cb_data[len("dislike_cancel:"):]
                    Thread(target=_handle_dislike_cancel_callback,
                           args=(token, chat_id, cq_id, msg_id, jid),
                           daemon=True).start()

                elif cb_data == "noop":
                    _answer_callback(token, cq_id)

                else:
                    _answer_callback(token, cq_id)

        except Exception as e:
            logger.warning(f"Telegram 轮询异常：{e}")
            time.sleep(5)


def start_polling() -> None:
    """后台启动 Telegram 回调监听，未配置则静默跳过。"""
    try:
        config = load_config()
        tg = config["notification"]["channels"]["telegram"]
        if not tg.get("enabled"):
            return
        token = tg.get("bot_token", "")
        chat_id = tg.get("chat_id", "")
        if not token or not chat_id:
            return
        Thread(target=_poll_loop, args=(token, chat_id), daemon=True).start()
        logger.info("Telegram 回调监听线程已启动")
    except Exception as e:
        logger.warning(f"Telegram 回调监听启动失败：{e}")
