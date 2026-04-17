from __future__ import annotations

import json
import re
import logging
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)


SKILL_MASTER_LIST = [
    # English — Tech
    "python", "javascript", "typescript", "java", "golang", "rust", "c++", "c#",
    "react", "vue", "angular", "node", "django", "flask", "fastapi", "spring",
    "sql", "postgresql", "mysql", "mongodb", "redis", "elasticsearch",
    "docker", "kubernetes", "aws", "gcp", "azure", "terraform",
    "machine learning", "deep learning", "nlp", "llm", "pytorch", "tensorflow",
    "data analysis", "data science", "spark", "hadoop", "airflow",
    "product manager", "project manager", "agile", "scrum",
    "figma", "ui", "ux", "design",
    "ios", "android", "swift", "kotlin", "flutter", "react native",
    "devops", "ci/cd", "git", "linux",
    # Finance / Quant (English)
    "bloomberg", "excel", "vba", "matlab", "quantitative", "risk management",
    "financial modeling", "derivatives", "fixed income", "equity", "portfolio",
    "murex", "kondor", "sophis", "trading", "pricing", "backtesting",
    "capital markets", "asset management", "hedge fund",
    # Italian
    "analisi dei dati", "apprendimento automatico", "gestione del progetto",
    "sviluppo software", "ingegneria", "finanza", "rischio", "modellazione",
    "previsione", "intelligenza artificiale", "trasformazione digitale",
    "mercati finanziari", "gestione del rischio",
    # French
    "analyse de données", "apprentissage automatique", "gestion de projet",
    "développement logiciel", "ingénierie", "finance", "risque", "modélisation",
    "prévision", "intelligence artificielle", "transformation digitale",
    "marchés financiers", "gestion des risques",
    # Chinese
    "数据分析", "机器学习", "深度学习", "项目管理", "软件开发", "人工智能",
    "风险管理", "量化", "金融建模", "后端", "前端", "全栈",
]


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    return json.loads(config_path.read_text())


def load_resume(config: dict) -> str:
    resume_file = config["matching"].get("resume_file", "resume.txt")
    resume_path = Path(__file__).parent / resume_file

    if not resume_path.exists():
        return ""

    # Try MarkItDown first — handles PDF/DOCX better than PyPDF2
    try:
        from markitdown import MarkItDown
        md = MarkItDown()
        result = md.convert(str(resume_path))
        if result.text_content.strip():
            return result.text_content
    except ImportError:
        logger.debug("markitdown 未安装，使用备用解析器（建议: pip install markitdown）")
    except Exception as e:
        logger.warning(f"MarkItDown 解析失败：{e}，使用备用解析器")

    suffix = resume_path.suffix.lower()

    if suffix == ".txt":
        return resume_path.read_text(encoding="utf-8", errors="ignore")

    if suffix == ".pdf":
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(str(resume_path))
            return " ".join(page.extract_text() or "" for page in reader.pages)
        except ImportError:
            logger.warning("PyPDF2 未安装，请运行: pip install markitdown")
            return ""

    if suffix in (".docx", ".doc"):
        try:
            from docx import Document
            doc = Document(str(resume_path))
            return " ".join(p.text for p in doc.paragraphs)
        except ImportError:
            logger.warning("python-docx 未安装，请运行: pip install markitdown")
            return ""

    return ""


def detect_language(text: str) -> str:
    """Lightweight language detection based on common function words."""
    text_lower = text.lower()
    italian = sum(1 for w in ["il ", "la ", "di ", "che ", "per ", "con ", "una ", "del "] if w in text_lower)
    french = sum(1 for w in ["le ", "la ", "de ", "et ", "les ", "des ", "pour ", "une "] if w in text_lower)
    chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")

    if chinese > 10:
        return "zh"
    if italian > french and italian > 3:
        return "it"
    if french > italian and french > 3:
        return "fr"
    return "en"


def extract_resume_skills(resume_text: str) -> list[str]:
    text = resume_text.lower()
    return [skill for skill in SKILL_MASTER_LIST if skill in text]


def match_with_keywords(job: dict, resume_skills: list[str]) -> Tuple[int, str]:
    text = f"{job.get('title','')} {job.get('company','')} {job.get('description','')}".lower()
    matched = [s for s in resume_skills if s in text]
    score = min(len(matched) * 5, 100)

    lang = detect_language(text)
    lang_note = ""
    if lang in ("it", "fr") and score < 30:
        lang_names = {"it": "意大利语", "fr": "法语"}
        lang_note = f"（检测到{lang_names[lang]} JD，建议在配置中切换为 AI 模式以获得更准确匹配）"

    reason = (f"匹配技能：{', '.join(matched)}" if matched else "未找到匹配技能")
    if lang_note:
        reason += f" {lang_note}"
    return score, reason


def match_with_ai(job: dict, config: dict) -> Tuple[bool, str]:
    ai_config = config["matching"]["ai"]
    provider = ai_config.get("provider", "claude").lower()
    api_key = ai_config.get("api_key", "")
    model = ai_config.get("model", "")

    # Merge per-location prompt (job domain) with global prompt (preferences)
    location_prompt = job.get("_user_prompt", "")
    global_prompt = ai_config.get("user_prompt", "")
    if location_prompt and global_prompt:
        user_prompt = f"{location_prompt}\n我的求职偏好：{global_prompt}"
    else:
        user_prompt = location_prompt or global_prompt

    if not api_key:
        raise ValueError(f"AI 匹配模式需要填写 {provider} 的 API Key")
    if not user_prompt:
        raise ValueError("AI 匹配模式需要填写你的求职要求描述（在 config.json 的 locations[].user_prompt 中配置）")

    # Load user feedback to guide filtering
    feedback_context = ""
    try:
        from storage import get_feedback_summary
        feedbacks = get_feedback_summary()
        if feedbacks:
            fb_lines = []
            for fb in feedbacks[:10]:
                company = fb.get("company") or ""
                title = fb.get("title") or ""
                code = fb.get("reason_code", "")
                text = fb.get("reason_text", "")
                entry = f"- [{code}] {title} @ {company}"
                if text:
                    entry += f"（{text}）"
                fb_lines.append(entry)
            feedback_context = (
                "\n\n## 用户历史反馈（近期不感兴趣的职位类型，请参考以避免类似推送）\n"
                + "\n".join(fb_lines)
            )
    except Exception:
        pass

    system = (
        "你是一个职位筛选助手。你的目标是：过滤掉客观上不可能匹配的职位，同时尽量不让用户错失有潜力的机会。\n"
        "职位描述可能是英文、中文、意大利文、法文或其他语言，请直接理解原文内容判断，无需翻译。\n\n"
        "## 第一层：硬门槛（以下任一命中 → push: false，不再继续评估）\n\n"
        "只有在 JD 中有【明确、不可协商的硬性表述】时才触发，以下是硬门槛类型：\n"
        "1. 地区不符：职位所在地与用户指定地区明显不同（不同国家或完全不同城市）\n"
        "2. 职位大类差距极大：职位核心职能与用户要求的行业/职种跨行业且毫无关联\n"
        "   例如：用户要电气工程师，职位是调酒师、厨师、驾驶员、护士等；\n"
        "   用户要金融咨询，职位是程序员、建筑工人等。\n"
        "   注意：同一大类下的细分差异（如：电气工程师 vs 电子工程师）不算硬门槛。\n"
        "3. 学历硬性要求：JD 明确写 required/mandatory/必须，且用户明显达不到\n"
        "   例如：「PhD required」而用户是本科；「必须具有医师执照」等。\n"
        "   若只写 preferred / plus / ideal，不算硬门槛。\n"
        "4. 公民身份/签证限制（必须严格过滤）：JD 中出现以下任何表述均触发此门槛：\n"
        "   - 「no visa sponsorship」「no sponsorship available」\n"
        "   - 「must be [国家] citizen」「[国家] citizenship required」\n"
        "   - 「must hold [国家] passport」「nationals only」\n"
        "   - 「must have right to work in [国家] without sponsorship」\n"
        "   - 「active security clearance required」（需公民身份才能持有）\n"
        "   - 「EU work permit required」「work authorisation required」且无赞助\n"
        "   - 意大利语：「cittadinanza italiana richiesta」「solo cittadini UE」\n"
        "   - 德语：「nur EU-Bürger」「Arbeitserlaubnis erforderlich」\n"
        "   注意：如果 JD 写「visa sponsorship available」或未提及签证，则不触发。\n"
        "5. 语言硬性要求：明确写「mother tongue / native speaker required」且是不可替代的工作语言\n"
        "6. 年龄/性别：JD 明确写了年龄上限或性别要求（少见但存在）\n\n"
        "## 第二层：软条件（宽松判断，拿不准就推送）\n\n"
        "以下情况一律推送，不作为拒绝理由：\n"
        "- 工作年限不足：要求5年用户有3年？推送，HR 经常弹性处理\n"
        "- 技能/工具部分缺失：可以学，不是拒绝理由\n"
        "- 行业经验不完全匹配：经验可迁移\n"
        "- 证书/资质缺失但非强制：可以考\n"
        "- 薪资范围：可谈\n"
        "- preferred / nice to have / plus 类表述：这类根本不算要求\n"
        "- 职位描述中的任何「加分项」\n\n"
        "## 总原则\n"
        "不确定是否应该拒绝？→ 推送。让用户自己判断是否值得申请，不要替用户放弃机会。\n"
        "但若用户历史反馈显示对某类职位不感兴趣，请适当提高该类职位的过滤门槛。\n\n"
        "只返回 JSON，格式：{\"push\": true/false, \"reason\": \"一句话理由，说明命中了哪条硬门槛（拒绝时）或软条件匹配情况（推送时）\"}\n"
        "不要输出任何其他内容。"
        + feedback_context
    )
    user_content = (
        f"我的求职要求：{user_prompt}\n\n"
        f"职位标题：{job.get('title','')}\n"
        f"公司：{job.get('company','')}\n"
        f"地点：{job.get('location','')}\n"
        f"描述：{job.get('description','')[:1000]}"
    )

    response_text = _call_ai(provider, api_key, model, system, user_content)

    try:
        clean = re.sub(r"```json|```", "", response_text).strip()
        data = json.loads(clean)
        return bool(data.get("push", False)), data.get("reason", "")
    except Exception:
        logger.warning(f"AI 返回解析失败：{response_text[:200]}")
        return False, "AI 返回格式异常"


def _call_ai(provider: str, api_key: str, model: str, system: str, user_content: str) -> str:
    import time

    retries = 3
    delay = 300  # 5 分钟

    for attempt in range(retries):
        try:
            return _call_ai_once(provider, api_key, model, system, user_content)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"AI 调用失败（第{attempt+1}次），{delay}s 后重试：{e}")
                time.sleep(delay)
            else:
                from notifier import notify_error
                notify_error("AI 匹配连续失败 3 次，请重启 Job Tracker")
                raise

    raise RuntimeError("不应到达此处")


def _call_ai_once(provider: str, api_key: str, model: str, system: str, user_content: str) -> str:
    if provider == "claude":
        import anthropic
        default_model = "claude-haiku-4-5-20251001"
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model or default_model,
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        return msg.content[0].text

    if provider == "openai":
        from openai import OpenAI
        default_model = "gpt-4o-mini"
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model or default_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            max_tokens=256,
        )
        return resp.choices[0].message.content

    if provider == "deepseek":
        from openai import OpenAI
        default_model = "deepseek-chat"
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        resp = client.chat.completions.create(
            model=model or default_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            max_tokens=256,
        )
        return resp.choices[0].message.content

    raise ValueError(f"不支持的 AI provider：{provider}，支持：claude / openai / deepseek")


def should_push(job: dict) -> Tuple[bool, int, str]:
    """Returns (push: bool, score: int, reason: str)"""
    config = load_config()
    mode = config["matching"]["mode"]

    if mode == "ai":
        try:
            push, reason = match_with_ai(job, config)
            return push, 0, reason
        except Exception as e:
            logger.error(f"AI 匹配失败：{e}")
            raise

    # keyword mode
    resume_text = load_resume(config)
    resume_skills = extract_resume_skills(resume_text) if resume_text else []
    score, reason = match_with_keywords(job, resume_skills)
    min_score = config["matching"].get("min_score", 70)
    return score >= min_score, score, reason
