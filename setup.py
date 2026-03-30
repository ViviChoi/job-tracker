from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional

from flask import Flask, render_template, request, jsonify, Response
from storage import (
    init_db, get_jobs, update_job, soft_delete_job,
    restore_job, permanent_delete_job, export_jobs
)

app = Flask(__name__)
CONFIG_PATH = Path(__file__).parent / "config.json"
PID_FILE = Path(__file__).parent / "data" / "tracker.pid"


def get_tracker_pid() -> Optional[int]:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)   # raises if process doesn't exist
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        PID_FILE.unlink(missing_ok=True)
        return None


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


def save_config(data: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))


@app.route("/")
def index():
    config = load_config()
    return render_template("setup.html", config=config)


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
def update_config():
    try:
        new_config = request.json
        old_config = load_config()

        # Preserve keys not managed by the setup UI
        for key in ("outreach", "resume_bot", "web_ui"):
            if key in old_config and key not in new_config:
                new_config[key] = old_config[key]

        # Detect storage mode switch and flag it
        old_storage = old_config["storage"]["mode"] if "mode" in old_config.get("storage", {}) else "local"
        new_storage = new_config["storage"].get("mode", "local")
        storage_switched = old_storage != new_storage

        save_config(new_config)

        if storage_switched:
            return jsonify({
                "success": True,
                "warning": f"存储方式已切换至 {new_storage}，历史去重记录不会迁移，近期可能出现少量重复推送"
            })

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/status", methods=["GET"])
def tracker_status():
    pid = get_tracker_pid()
    return jsonify({"running": pid is not None, "pid": pid})


def _get_resume_status() -> dict:
    """检查简历文件是否存在，返回状态信息。"""
    cfg = load_config()
    resume_file = cfg.get("matching", {}).get("resume_file", "resume.txt")
    resume_path = Path(__file__).parent / resume_file
    exists = resume_path.exists()
    return {
        "exists": exists,
        "filename": resume_file if exists else None,
    }


@app.route("/api/resume_status", methods=["GET"])
def resume_status():
    return jsonify(_get_resume_status())


@app.route("/api/start", methods=["POST"])
def start_tracker():
    if get_tracker_pid() is not None:
        return jsonify({"success": False, "error": "Job Tracker 已在运行中"})

    # 关键词模式必须有简历
    cfg = load_config()
    mode = cfg.get("matching", {}).get("mode", "keywords")
    status = _get_resume_status()
    if mode == "keywords" and not status["exists"]:
        return jsonify({
            "success": False,
            "error": "❌ 未检测到简历文件\n\n请先在「简历上传」页面上传简历（.pdf / .txt / .docx）\n关键词匹配模式需要从简历中提取技能词，缺少简历则无法匹配职位"
        })

    try:
        main_py = Path(__file__).parent / "main.py"
        proc = subprocess.Popen(
            [sys.executable, str(main_py)],
            cwd=str(Path(__file__).parent),
            start_new_session=True,
        )
        PID_FILE.write_text(str(proc.pid))
        return jsonify({"success": True, "message": f"Job Tracker 已启动（PID {proc.pid}）"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/stop", methods=["POST"])
def stop_tracker():
    pid = get_tracker_pid()
    if pid is None:
        return jsonify({"success": False, "error": "Job Tracker 未在运行"})
    try:
        os.kill(pid, signal.SIGTERM)
        PID_FILE.unlink(missing_ok=True)
        return jsonify({"success": True, "message": "Job Tracker 已停止"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/upload_resume", methods=["POST"])
def upload_resume():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "未收到文件"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"success": False, "error": "文件名为空"}), 400

    allowed = {".txt", ".pdf", ".docx"}
    suffix = Path(file.filename).suffix.lower()
    if suffix not in allowed:
        return jsonify({"success": False, "error": f"不支持的格式，请上传 {'/'.join(allowed)}"}), 400

    save_path = Path(__file__).parent / f"resume{suffix}"
    file.save(str(save_path))

    config = load_config()
    config["matching"]["resume_file"] = f"resume{suffix}"
    save_config(config)

    return jsonify({"success": True, "message": f"简历已保存：resume{suffix}"})


@app.route("/api/jobs", methods=["GET"])
def list_jobs():
    jobs = get_jobs(
        sort_by=request.args.get("sort_by", "created_at"),
        sort_dir=request.args.get("sort_dir", "desc"),
        filter_company=request.args.get("company", ""),
        filter_status=request.args.get("status", ""),
        filter_starred=request.args.get("starred") == "1",
        filter_date_from=request.args.get("date_from", ""),
        filter_date_to=request.args.get("date_to", ""),
        trash=request.args.get("trash") == "1",
    )
    return jsonify(jobs)


@app.route("/api/jobs/<job_id>", methods=["PATCH"])
def patch_job(job_id):
    try:
        update_job(job_id, request.json)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/jobs/<job_id>/delete", methods=["POST"])
def delete_job(job_id):
    soft_delete_job(job_id)
    return jsonify({"success": True})


@app.route("/api/jobs/<job_id>/restore", methods=["POST"])
def restore_job_route(job_id):
    restore_job(job_id)
    return jsonify({"success": True})


@app.route("/api/jobs/<job_id>/permanent_delete", methods=["POST"])
def perm_delete_job(job_id):
    permanent_delete_job(job_id)
    return jsonify({"success": True})


@app.route("/api/alias_cache", methods=["GET"])
def get_alias_cache():
    from alias_learner import load_cache
    data_dir = str(Path(__file__).parent / "data")
    return jsonify(load_cache(data_dir))


@app.route("/api/alias_learn", methods=["POST"])
def trigger_alias_learn():
    """Manually trigger learning for all current keywords."""
    data = request.json or {}
    keywords = data.get("keywords", [])
    location = data.get("location", "")
    config = load_config()
    al = config.get("alias_learning", {})

    if not al.get("enabled") or not al.get("api_key"):
        return jsonify({"success": False, "error": "别名学习未启用或缺少 API Key"}), 400
    if not keywords:
        return jsonify({"success": False, "error": "没有关键词"}), 400

    from alias_learner import detect_languages_from_location, trigger_learning
    data_dir = str(Path(__file__).parent / "data")
    languages = detect_languages_from_location(location)

    for kw in keywords:
        trigger_learning(data_dir, kw, languages, al["provider"], al["api_key"], al.get("model", ""))

    return jsonify({"success": True, "message": f"已在后台学习 {len(keywords)} 个关键词（语言：{languages}）"})


@app.route("/api/suggest_aliases", methods=["POST"])
def suggest_aliases():
    data = request.json or {}
    keyword = data.get("keyword", "").strip()
    if not keyword:
        return jsonify({"success": False, "error": "关键词为空"}), 400

    config = load_config()
    # Prefer alias_learning API key; fall back to matching AI key
    al = config.get("alias_learning", {})
    ai = config.get("matching", {}).get("ai", {})
    api_key = al.get("api_key", "") or ai.get("api_key", "")
    provider = al.get("provider", "") or ai.get("provider", "claude")
    model = al.get("model", "") or ai.get("model", "")

    if not api_key:
        return jsonify({"success": False, "error": "请先在「别名学习」Tab 中填写 API Key（或在「匹配设置」中选择 AI 模式并填写 API Key）"}), 400

    system = (
        "你是一个求职搜索专家。给定一个职位关键词，生成该职位在各语言和行业中的同义词、别名和常见变体，"
        "用于扩展 LinkedIn 搜索覆盖面。\n\n"
        "要求：\n"
        "1. 涵盖英文、中文及其他相关语言（根据关键词语言判断）\n"
        "2. 包括行业内常见的不同叫法（如 Developer / Programmer / Engineer）\n"
        "3. 包括缩写和简称（如 PM、SWE、UX）\n"
        "4. 只返回 JSON 数组，每项是一个字符串，最多 8 个\n"
        "5. 不要包含原词本身\n\n"
        "格式：[\"alias1\", \"alias2\", ...]"
    )
    user = f"关键词：{keyword}"

    try:
        from matcher import _call_ai
        result_text = _call_ai(provider, api_key, model, system, user)
        import re
        clean = re.sub(r"```json|```", "", result_text).strip()
        aliases = json.loads(clean)
        if not isinstance(aliases, list):
            raise ValueError("返回格式不是数组")
        return jsonify({"success": True, "aliases": [str(a) for a in aliases[:8]]})
    except Exception as e:
        return jsonify({"success": False, "error": f"AI 建议失败：{e}"}), 500


@app.route("/api/jobs/export", methods=["GET"])
def export_jobs_route():
    fmt = request.args.get("fmt", "csv")
    trash = request.args.get("trash") == "1"
    content = export_jobs(fmt=fmt, trash=trash)
    mimetype = "application/json" if fmt == "json" else "text/csv"
    filename = f"jobs_export.{fmt}"
    return Response(
        content,
        mimetype=mimetype,
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8080, debug=False)
