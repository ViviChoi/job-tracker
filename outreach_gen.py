"""
Outreach Generator — 用 Claude Opus 生成个性化 LinkedIn 联系消息
同时提供 Flask REST API（默认端口 8082）
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Thread
from typing import Optional

import threading

import anthropic

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent
_RESUME_CACHE: Optional[str] = None
_RESUME_LOCK = threading.Lock()

# Opus pricing (per million tokens)
_INPUT_PRICE_PER_M = 15.0
_OUTPUT_PRICE_PER_M = 75.0


# ── Config & Resume helpers ───────────────────────────────────

def _load_config() -> dict:
    return json.loads((_BASE_DIR / "config.json").read_text(encoding="utf-8"))


def _load_resume() -> str:
    global _RESUME_CACHE
    with _RESUME_LOCK:
        if _RESUME_CACHE is not None:
            return _RESUME_CACHE

        config = _load_config()
        resume_file = config["matching"].get("resume_file", "resume.txt")
        resume_path = _BASE_DIR / resume_file

        if not resume_path.exists():
            logger.warning(f"简历文件未找到：{resume_path}")
            _RESUME_CACHE = ""
            return ""

        try:
            suffix = resume_path.suffix.lower()
            if suffix == ".pdf":
                import PyPDF2
                reader = PyPDF2.PdfReader(str(resume_path))
                content = "\n".join(p.extract_text() or "" for p in reader.pages)
            elif suffix in (".doc", ".docx"):
                import docx
                doc = docx.Document(str(resume_path))
                content = "\n".join(p.text for p in doc.paragraphs)
            else:
                content = resume_path.read_text(encoding="utf-8", errors="ignore")

            _RESUME_CACHE = content.strip()
            return _RESUME_CACHE
        except Exception as e:
            logger.error(f"简历读取失败：{e}")
            _RESUME_CACHE = ""
            return ""


def _get_client(config: dict) -> anthropic.Anthropic:
    import os
    # 优先级：环境变量 > config outreach.api_key > config matching.ai.api_key
    api_key = (
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or config.get("outreach", {}).get("api_key", "").strip()
        or config.get("matching", {}).get("ai", {}).get("api_key", "").strip()
    )
    if not api_key:
        raise ValueError("未配置 Claude API Key（环境变量 ANTHROPIC_API_KEY 或 config outreach.api_key）")
    return anthropic.Anthropic(api_key=api_key)


# ── Core generation ───────────────────────────────────────────

def generate_outreach(
    contact: dict,
    job: dict,
    model: Optional[str] = None,
) -> dict:
    """
    Generate a personalized LinkedIn outreach message using Claude Opus.

    Args:
        contact: {name, title, linkedin_url, snippet, ...}
        job: {title, company, location, description, link, ...}
        model: Claude model override (defaults to config outreach.model or claude-opus-4-6)

    Returns:
        {success, message, model, input_tokens, output_tokens, cost_usd} or {success, error}
    """
    config = _load_config()
    outreach_cfg = config.get("outreach", {})
    resolved_model = model or outreach_cfg.get("model", "claude-opus-4-6")

    resume = _load_resume()
    if not resume:
        return {"success": False, "error": "未找到简历，请检查 config.json 中的 matching.resume_file"}

    name = contact.get("name") or "招聘负责人"
    title = contact.get("title", "")
    snippet = contact.get("snippet", "")
    li_url = contact.get("linkedin_url", "")

    job_title = job.get("title", "")
    job_company = job.get("company", "")
    job_location = job.get("location", "")
    job_desc = (job.get("description") or "")[:800]

    system_prompt = (
        "你是一位求职策略专家，擅长撰写高转化率的 LinkedIn 联系消息。\n"
        "目标：根据招聘人员的背景和求职者简历，生成一条能让招聘人员主动关注并推荐该求职者的个性化消息。\n\n"
        "消息写作要求：\n"
        "1. 不超过 280 字（符合 LinkedIn 连接请求字数限制）\n"
        "2. 开头用一句具体细节展示你真的了解对方（不要用套话）\n"
        "3. 简洁点出求职者最匹配该职位的 1-2 个核心优势\n"
        "4. 结尾有明确行动号召（如：希望能进一步交流）\n"
        "5. 语气真诚自然，不要过于推销\n"
        "6. 根据招聘人员所在地区决定使用语言：德国→德语，意大利→意大利语，其他→英语"
    )

    user_prompt = (
        f"# 招聘人员信息\n"
        f"姓名：{name}\n"
        f"职位：{title}\n"
        f"LinkedIn 页面摘要：{snippet}\n"
        f"LinkedIn 主页：{li_url}\n\n"
        f"# 目标职位\n"
        f"职位名称：{job_title}\n"
        f"公司：{job_company}\n"
        f"地点：{job_location}\n"
        f"职位描述（摘要）：{job_desc}\n\n"
        f"# 我的简历\n{resume}\n\n"
        f"请生成一条 LinkedIn 联系消息，发给 {name}，让他/她更可能关注并帮助推进我的申请。\n"
        f"只输出消息正文，不需要任何解释。"
    )

    try:
        client = _get_client(config)
        response = client.messages.create(
            model=resolved_model,
            max_tokens=600,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        message_text = response.content[0].text.strip()
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost = (input_tokens * _INPUT_PRICE_PER_M + output_tokens * _OUTPUT_PRICE_PER_M) / 1_000_000

        return {
            "success": True,
            "message": message_text,
            "model": resolved_model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost, 5),
        }
    except Exception as e:
        logger.error(f"Outreach 生成失败：{e}")
        return {"success": False, "error": str(e)}


# ── Flask API ─────────────────────────────────────────────────

def _create_app():
    from flask import Flask, request, jsonify

    app = Flask(__name__)

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "service": "outreach-api"})

    @app.route("/api/find-hr", methods=["POST"])
    def api_find_hr():
        """
        Find HR contacts at a company.
        Body: {"company": "Bosch", "max_results": 5}
        """
        data = request.get_json(silent=True) or {}
        company = (data.get("company") or "").strip()
        try:
            max_results = min(int(data.get("max_results", 5)), 10)
        except (ValueError, TypeError):
            return jsonify({"success": False, "error": "max_results 必须为整数"}), 400

        if not company:
            return jsonify({"success": False, "error": "缺少 company 字段"}), 400

        try:
            from hr_finder import find_hr_contacts
            contacts = find_hr_contacts(company, max_results=max_results)
            return jsonify({"success": True, "company": company, "contacts": contacts})
        except Exception as e:
            logger.error(f"/api/find-hr 异常：{e}")
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/outreach", methods=["POST"])
    def api_outreach():
        """
        Generate personalized LinkedIn outreach message.
        Body: {
            "contact": {name, title, linkedin_url, snippet},
            "job": {title, company, location, description, link},
            "model": "claude-opus-4-6"  // optional
        }
        Response: {success, message, model, input_tokens, output_tokens, cost_usd}
        """
        data = request.get_json(silent=True) or {}
        contact = data.get("contact")
        job = data.get("job", {})
        model = data.get("model")

        if not contact:
            return jsonify({"success": False, "error": "缺少 contact 字段"}), 400

        result = generate_outreach(contact, job, model=model)
        status = 200 if result.get("success") else 500
        return jsonify(result), status

    return app


def start_api_server(port: int = 8082) -> None:
    """Start Outreach API Flask server in a background thread."""
    def _run():
        app = _create_app()
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

    t = Thread(target=_run, daemon=True, name="outreach-api")
    t.start()
    logger.info(f"Outreach API 已启动：http://localhost:{port}")
    logger.info(f"  POST /api/find-hr    → 搜索公司 HR 联系人")
    logger.info(f"  POST /api/outreach   → 生成个性化联系消息（默认 claude-opus-4-6）")
