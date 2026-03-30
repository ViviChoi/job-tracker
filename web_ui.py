"""
Job Tracker Web UI
运行：python web_ui.py
访问：http://localhost:8083
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from storage import get_feedback_summary, get_jobs, init_db, save_feedback, soft_delete_job, update_job

logger = logging.getLogger(__name__)

app = Flask(__name__)

def _load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    return json.loads(config_path.read_text())


def _save_config(config: dict) -> None:
    config_path = Path(__file__).parent / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))


def _fetch_jd_text(url: str) -> str:
    """Fetch a JD URL and return stripped plain text (max 4000 chars)."""
    from html.parser import HTMLParser
    import requests as req

    class _Extractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self._parts: list = []
            self._skip_depth: int = 0
            self._skip_tags = {"script", "style", "nav", "footer", "header", "noscript"}

        def handle_starttag(self, tag: str, attrs: list) -> None:
            if tag in self._skip_tags:
                self._skip_depth += 1

        def handle_endtag(self, tag: str) -> None:
            if tag in self._skip_tags and self._skip_depth > 0:
                self._skip_depth -= 1

        def handle_data(self, data: str) -> None:
            if self._skip_depth == 0 and data.strip():
                self._parts.append(data.strip())

        def get_text(self) -> str:
            return " ".join(self._parts)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    resp = req.get(url, headers=headers, timeout=15, allow_redirects=True)
    resp.raise_for_status()
    extractor = _Extractor()
    extractor.feed(resp.text)
    raw = extractor.get_text()
    cleaned = re.sub(r"\s+", " ", raw).strip()
    return cleaned[:4000]


_DISLIKE_REASONS = [
    ("not_relevant",   "工作内容不相关"),
    ("wrong_location", "地点不符合"),
    ("citizenship",    "需要公民身份/签证"),
    ("too_senior",     "级别要求太高"),
    ("wrong_field",    "行业/职种方向不对"),
    ("other",          "其他原因"),
]

_HTML = """
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job Tracker</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f5f5f7; color: #1d1d1f; font-size: 14px; }
  header { background: #fff; border-bottom: 1px solid #e0e0e0; padding: 14px 24px;
           display: flex; align-items: center; gap: 16px; position: sticky; top: 0; z-index: 10; }
  header h1 { font-size: 18px; font-weight: 600; }
  .badge { background: #0071e3; color: #fff; border-radius: 10px;
           padding: 2px 8px; font-size: 12px; }
  .filters { display: flex; gap: 10px; flex-wrap: wrap; padding: 14px 24px;
             background: #fff; border-bottom: 1px solid #e0e0e0; }
  .filters input, .filters select { padding: 6px 10px; border: 1px solid #ccc;
             border-radius: 6px; font-size: 13px; background: #fafafa; }
  .filters button { padding: 6px 14px; border: none; border-radius: 6px;
             background: #0071e3; color: #fff; cursor: pointer; font-size: 13px; }
  .container { max-width: 1100px; margin: 0 auto; padding: 20px 24px; }
  .tabs { display: flex; gap: 2px; margin-bottom: 18px; }
  .tab { padding: 7px 18px; border-radius: 8px; cursor: pointer;
         background: #e8e8ed; font-size: 13px; border: none; }
  .tab.active { background: #0071e3; color: #fff; }
  .jobs-grid { display: flex; flex-direction: column; gap: 10px; }
  .job-card { background: #fff; border-radius: 12px; padding: 16px 18px;
              border: 1px solid #e0e0e0; display: flex; align-items: flex-start;
              gap: 14px; transition: box-shadow .15s, opacity .15s; }
  .job-card:hover { box-shadow: 0 2px 12px rgba(0,0,0,.08); }
  .job-card.is-disliked { opacity: 0.45; }
  .job-card.is-disliked:hover { opacity: 0.75; }
  .job-info { flex: 1; min-width: 0; }
  .job-title { font-weight: 600; font-size: 15px; margin-bottom: 4px;
               white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .job-meta { color: #666; font-size: 12px; margin-bottom: 6px; }
  .job-reason { font-size: 12px; color: #444; background: #f0f4ff;
                border-radius: 6px; padding: 4px 8px; display: inline-block; margin-top: 4px; }
  .job-actions { display: flex; gap: 8px; align-items: center; flex-shrink: 0; }
  .btn { padding: 5px 12px; border-radius: 7px; border: 1px solid #ccc;
         background: #fff; cursor: pointer; font-size: 12px; }
  .btn-dislike { border-color: #ff9500; color: #ff9500; }
  .btn-dislike:hover { background: #fff7ec; }
  .btn-link { border-color: #0071e3; color: #0071e3; text-decoration: none; }
  .btn-link:hover { background: #f0f7ff; }
  .status-badge { font-size: 11px; padding: 2px 8px; border-radius: 8px; }
  .status-new { background: #e3f2fd; color: #1565c0; }
  .status-applied { background: #e8f5e9; color: #2e7d32; }
  .status-resume_generated { background: #f3e5f5; color: #6a1b9a; }
  .status-disliked { background: #ffeee8; color: #c62828; }

  /* Dislike modal */
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.4);
                   z-index: 100; align-items: center; justify-content: center; }
  .modal-overlay.open { display: flex; }
  .modal { background: #fff; border-radius: 14px; padding: 24px; width: 340px;
           max-width: 90vw; }
  .modal h3 { font-size: 16px; margin-bottom: 16px; }
  .reason-list { display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px; }
  .reason-btn { padding: 10px 14px; border: 1px solid #e0e0e0; border-radius: 8px;
                background: #fafafa; cursor: pointer; text-align: left; font-size: 13px; }
  .reason-btn:hover { background: #f0f7ff; border-color: #0071e3; }
  .reason-btn.selected { background: #e3f2fd; border-color: #0071e3; color: #0071e3; }
  .modal-footer { display: flex; gap: 8px; justify-content: flex-end; }
  .btn-cancel { padding: 8px 16px; border: 1px solid #ccc; border-radius: 8px;
                background: #fff; cursor: pointer; }
  .btn-submit { padding: 8px 16px; border: none; border-radius: 8px;
                background: #0071e3; color: #fff; cursor: pointer; }
  .btn-submit:disabled { background: #ccc; cursor: default; }

  /* Feedback tab */
  .feedback-table { width: 100%; border-collapse: collapse; background: #fff;
                    border-radius: 12px; overflow: hidden; }
  .feedback-table th, .feedback-table td { padding: 10px 14px; text-align: left;
                    border-bottom: 1px solid #f0f0f0; font-size: 13px; }
  .feedback-table th { background: #f5f5f7; font-weight: 600; }
  .empty { text-align: center; color: #999; padding: 40px; }

  /* Preferences tab */
  .pref-section { display: flex; flex-direction: column; gap: 16px; }
  .pref-card { background: #fff; border-radius: 14px; border: 1px solid #e0e0e0;
               overflow: hidden; }
  .pref-card-header { display: flex; align-items: center; gap: 10px; padding: 14px 18px;
                      background: #fafafa; border-bottom: 1px solid #f0f0f0; }
  .pref-card-header h3 { font-size: 14px; font-weight: 600; flex: 1; }
  .pref-card-body { padding: 16px 18px; display: flex; flex-direction: column; gap: 14px; }
  .pref-section-label { font-size: 11px; font-weight: 700; color: #888;
                        text-transform: uppercase; letter-spacing: .5px; margin-bottom: 4px; }
  .pref-desc { font-size: 12px; color: #999; margin-bottom: 6px; }
  .pref-textarea { width: 100%; min-height: 80px; padding: 10px; border: 1px solid #ddd;
                   border-radius: 8px; font-size: 13px; font-family: inherit; resize: vertical;
                   background: #fafafa; line-height: 1.5; }
  .pref-textarea:focus { outline: none; border-color: #0071e3; background: #fff; }
  .pref-actions { display: flex; gap: 10px; margin-top: 4px; align-items: center; }
  .btn-save { padding: 8px 20px; border: none; border-radius: 8px;
              background: #0071e3; color: #fff; cursor: pointer; font-size: 13px; }
  .btn-optimize { padding: 8px 20px; border: 1px solid #34c759; border-radius: 8px;
                  background: #fff; color: #34c759; cursor: pointer; font-size: 13px; }
  .btn-optimize:hover { background: #f0fff4; }
  .btn-optimize:disabled { opacity: .5; cursor: default; }
  .save-hint { font-size: 12px; color: #34c759; }
  .optimize-panel { background: #f0fff4; border: 1px solid #34c759; border-radius: 12px;
                    padding: 18px; margin-top: 16px; display: none; }
  .optimize-panel.visible { display: block; }
  .optimize-panel h4 { font-size: 13px; font-weight: 600; color: #1d7a3a; margin-bottom: 10px; }
  .optimize-item { margin-bottom: 14px; }
  .optimize-item label { font-size: 12px; color: #555; display: block; margin-bottom: 4px; font-weight: 600; }
  .btn-apply { padding: 7px 16px; border: none; border-radius: 8px;
               background: #34c759; color: #fff; cursor: pointer; font-size: 13px; }
  /* Blacklist section */
  .bl-section { background: #fff8f8; border: 1px solid #ffe0e0; border-radius: 10px;
                padding: 12px 14px; }
  .bl-section .pref-section-label { color: #c0392b; }
  .bl-row { display: flex; gap: 10px; margin-top: 8px; }
  .bl-col { flex: 1; }
  .bl-col label { font-size: 12px; color: #555; font-weight: 600; display: block; margin-bottom: 5px; }
  .bl-textarea { width: 100%; height: 72px; padding: 8px 10px; border: 1px solid #ddd;
                 border-radius: 7px; font-size: 12px; font-family: inherit; resize: vertical;
                 background: #fff; line-height: 1.5; }
  .bl-textarea:focus { outline: none; border-color: #e74c3c; background: #fff; }
  .bl-hint { font-size: 11px; color: #aaa; margin-top: 3px; }
  .jd-learn-row { display: flex; gap: 8px; margin-top: 10px; align-items: center; }
  .jd-url-input { flex: 1; padding: 7px 10px; border: 1px solid #ccc; border-radius: 7px;
                  font-size: 12px; background: #fafafa; }
  .jd-url-input:focus { outline: none; border-color: #5856d6; background: #fff; }
  .btn-jd-learn { padding: 7px 14px; border: none; border-radius: 7px;
                  background: #5856d6; color: #fff; cursor: pointer; font-size: 12px;
                  white-space: nowrap; }
  .btn-jd-learn:disabled { opacity: .5; cursor: default; }
  .jd-suggest-box { background: #f0f0ff; border: 1px solid #5856d6; border-radius: 8px;
                    padding: 12px; margin-top: 8px; }
  .jd-suggest-label { font-size: 11px; color: #5856d6; font-weight: 600; margin-bottom: 6px; }
  .jd-suggest-text { font-size: 12px; color: #333; line-height: 1.5; margin-bottom: 8px;
                     white-space: pre-wrap; }
  .jd-suggest-actions { display: flex; gap: 8px; }
  .btn-jd-apply { padding: 5px 12px; border: none; border-radius: 6px;
                  background: #5856d6; color: #fff; cursor: pointer; font-size: 12px; }
  .btn-jd-merge { padding: 5px 12px; border: 1px solid #5856d6; border-radius: 6px;
                  background: #fff; color: #5856d6; cursor: pointer; font-size: 12px; }
  .btn-jd-dismiss { padding: 5px 12px; border: 1px solid #ccc; border-radius: 6px;
                    background: #fff; color: #999; cursor: pointer; font-size: 12px; }
  /* Location city/country inputs */
  .loc-inputs { display: flex; gap: 10px; margin-bottom: 4px; }
  .loc-field { flex: 1; display: flex; flex-direction: column; gap: 4px; }
  .loc-field label { font-size: 11px; font-weight: 700; color: #888;
                     text-transform: uppercase; letter-spacing: .5px; }
  .loc-input { padding: 8px 10px; border: 1px solid #ddd; border-radius: 8px;
               font-size: 13px; font-family: inherit; background: #fafafa;
               transition: border-color .15s; }
  .loc-input:focus { outline: none; border-color: #0071e3; background: #fff; }
  .loc-input.error { border-color: #e74c3c; background: #fff8f8; }
  .loc-error { font-size: 11px; color: #e74c3c; min-height: 16px; }
</style>
</head>
<body>
<header>
  <h1>💼 Job Tracker</h1>
  <span class="badge" id="job-count">—</span>
  <span style="color:#999; font-size:12px; margin-left:auto">http://localhost:8083</span>
</header>

<div class="filters">
  <input type="text" id="filter-company" placeholder="搜索公司…" oninput="applyFilters()">
  <select id="filter-status" onchange="applyFilters()">
    <option value="">所有状态</option>
    <option value="new">新</option>
    <option value="resume_generated">简历已生成</option>
    <option value="applied">已投递</option>
    <option value="rejected">已拒绝</option>
    <option value="disliked">不感兴趣</option>
  </select>
  <button onclick="applyFilters()">刷新</button>
</div>

<div class="container">
  <div class="tabs">
    <button class="tab active" onclick="switchTab('jobs', this)">职位列表</button>
    <button class="tab" onclick="switchTab('feedback', this)">用户反馈</button>
    <button class="tab" onclick="switchTab('preferences', this)">偏好设置</button>
  </div>

  <div id="tab-jobs">
    <div class="jobs-grid" id="jobs-list"></div>
    <div class="empty" id="jobs-empty" style="display:none">暂无职位</div>
  </div>

  <div id="tab-preferences" style="display:none">
    <div class="pref-section" id="pref-locations"></div>
    <div class="pref-actions">
      <button class="btn-save" onclick="savePreferences()">保存偏好</button>
      <button class="btn-optimize" id="btn-optimize" onclick="optimizePreferences()">✨ AI分析优化</button>
      <span class="save-hint" id="pref-save-hint"></span>
    </div>
    <div class="optimize-panel" id="optimize-panel">
      <h4>✨ AI 优化建议（基于历史反馈）</h4>
      <div id="optimize-content"></div>
      <button class="btn-apply" onclick="applyOptimized()">应用建议</button>
      <button class="btn-cancel" onclick="document.getElementById('optimize-panel').classList.remove('visible')" style="margin-left:8px">忽略</button>
    </div>
  </div>

  <div id="tab-feedback" style="display:none">
    <table class="feedback-table">
      <thead><tr><th>职位</th><th>公司</th><th>原因</th><th>时间</th></tr></thead>
      <tbody id="feedback-body"></tbody>
    </table>
    <div class="empty" id="feedback-empty" style="display:none">暂无反馈记录</div>
  </div>
</div>

<!-- Dislike modal -->
<div class="modal-overlay" id="dislike-modal">
  <div class="modal">
    <h3>👎 减少推荐 — 请选择原因</h3>
    <div class="reason-list" id="reason-list"></div>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="closeModal()">取消</button>
      <button class="btn-submit" id="submit-reason" disabled onclick="submitDislike()">确认</button>
    </div>
  </div>
</div>

<script>
const REASONS = {{ reasons_json }};
let currentJobId = null;
let selectedReason = null;
let allJobs = [];

async function loadJobs() {
  const company = document.getElementById('filter-company').value;
  const status = document.getElementById('filter-status').value;
  let url = '/api/jobs?';
  if (company) url += `company=${encodeURIComponent(company)}&`;
  if (status) url += `status=${encodeURIComponent(status)}&`;
  const res = await fetch(url);
  const data = await res.json();
  allJobs = data.jobs || [];
  renderJobs(allJobs);
}

function renderJobs(jobs) {
  const list = document.getElementById('jobs-list');
  const empty = document.getElementById('jobs-empty');
  document.getElementById('job-count').textContent = jobs.length + ' 条';
  if (!jobs.length) { list.innerHTML = ''; empty.style.display = ''; return; }
  empty.style.display = 'none';
  list.innerHTML = jobs.map(j => {
    const status = j.status || 'new';
    const statusLabel = {new:'新', resume_generated:'简历已生成', applied:'已投递',
                         resume_generating:'生成中', rejected:'已拒绝',
                         disliked:'不感兴趣'}[status] || status;
    const dislikedClass = status === 'disliked' ? ' is-disliked' : '';
    return `
    <div class="job-card${dislikedClass}" id="job-${j.id}">
      <div class="job-info">
        <div class="job-title" title="${esc(j.title)}">${esc(j.title)}</div>
        <div class="job-meta">🏢 ${esc(j.company)} &nbsp;·&nbsp; 📍 ${esc(j.location)} &nbsp;·&nbsp; 🕐 ${(j.posted_at||'').slice(0,10)}</div>
        ${j.match_reason ? `<div class="job-reason">✅ ${esc(j.match_reason)}</div>` : ''}
      </div>
      <div class="job-actions">
        <span class="status-badge status-${status}">${statusLabel}</span>
        <a href="${esc(j.link)}" target="_blank" class="btn btn-link">查看</a>
        ${status !== 'disliked' ? `<button class="btn btn-dislike" onclick="openDislike('${j.id}')">👎</button>` : ''}
      </div>
    </div>`;
  }).join('');
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function applyFilters() { loadJobs(); }

function openDislike(jobId) {
  currentJobId = jobId;
  selectedReason = null;
  document.getElementById('submit-reason').disabled = true;
  const list = document.getElementById('reason-list');
  list.innerHTML = REASONS.map(([code, label]) =>
    `<button class="reason-btn" onclick="selectReason('${code}', this)">${label}</button>`
  ).join('');
  document.getElementById('dislike-modal').classList.add('open');
}

function selectReason(code, el) {
  selectedReason = code;
  document.querySelectorAll('.reason-btn').forEach(b => b.classList.remove('selected'));
  el.classList.add('selected');
  document.getElementById('submit-reason').disabled = false;
}

function closeModal() {
  document.getElementById('dislike-modal').classList.remove('open');
  currentJobId = null;
  selectedReason = null;
}

async function submitDislike() {
  if (!currentJobId || !selectedReason) return;
  const res = await fetch('/api/feedback', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({job_id: currentJobId, reason_code: selectedReason}),
  });
  const data = await res.json();
  closeModal();
  if (data.ok) {
    const card = document.getElementById('job-' + currentJobId);
    if (card) card.style.opacity = '0.4';
  }
}

async function loadFeedback() {
  const res = await fetch('/api/feedback');
  const data = await res.json();
  const tbody = document.getElementById('feedback-body');
  const empty = document.getElementById('feedback-empty');
  const items = data.feedback || [];
  if (!items.length) { tbody.innerHTML = ''; empty.style.display = ''; return; }
  empty.style.display = 'none';
  tbody.innerHTML = items.map(f => `
    <tr>
      <td>${esc(f.title||'—')}</td>
      <td>${esc(f.company||'—')}</td>
      <td>${esc(f.reason_text||f.reason_code)}</td>
      <td>${(f.created_at||'').slice(0,16)}</td>
    </tr>`).join('');
}

function switchTab(name, el) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('tab-jobs').style.display = name === 'jobs' ? '' : 'none';
  document.getElementById('tab-feedback').style.display = name === 'feedback' ? '' : 'none';
  document.getElementById('tab-preferences').style.display = name === 'preferences' ? '' : 'none';
  if (name === 'feedback') loadFeedback();
  if (name === 'preferences') loadPreferences();
}

// ── Preferences ──────────────────────────────────────────────
let preferencesData = null;
let optimizedData = null;

async function loadPreferences() {
  const res = await fetch('/api/preferences');
  const data = await res.json();
  preferencesData = data;
  const container = document.getElementById('pref-locations');

  const gbl = data.global_blacklist || {companies: [], keywords: []};
  let html = `
    <div class="pref-card">
      <div class="pref-card-header"><h3>🌐 全局偏好</h3></div>
      <div class="pref-card-body">
        <div>
          <div class="pref-section-label">通用求职偏好</div>
          <p class="pref-desc">薪资、工作方式等通用偏好，适用于所有地区</p>
          <textarea class="pref-textarea" id="pref-global">${esc(data.global_prompt)}</textarea>
        </div>
        <div class="bl-section">
          <div class="pref-section-label">🚫 全局黑名单</div>
          <p class="pref-desc">所有地区均生效，每行一条</p>
          <div class="bl-row">
            <div class="bl-col">
              <label>屏蔽公司</label>
              <textarea class="bl-textarea" id="pref-gbl-companies">${esc((gbl.companies||[]).join('\n'))}</textarea>
              <div class="bl-hint">公司名称含该词即屏蔽（不区分大小写）</div>
            </div>
            <div class="bl-col">
              <label>屏蔽关键词</label>
              <textarea class="bl-textarea" id="pref-gbl-keywords">${esc((gbl.keywords||[]).join('\n'))}</textarea>
              <div class="bl-hint">职位标题含该词即屏蔽（不区分大小写）</div>
            </div>
          </div>
        </div>
      </div>
    </div>`;

  (data.locations || []).forEach((loc, i) => {
    const bl = loc.blacklist || {companies: [], keywords: []};
    html += `
    <div class="pref-card">
      <div class="pref-card-header">
        <h3>📍 ${esc(loc.name)}</h3>
      </div>
      <div class="pref-card-body">
        <div>
          <div class="pref-section-label">搜索地区</div>
          <p class="pref-desc">必须同时填写城市和国家，避免同名城市混淆（如：Milan 需注明 Italy，否则可能抓到美国的 Milano, TX）</p>
          <div class="loc-inputs">
            <div class="loc-field">
              <label>城市</label>
              <input class="loc-input" id="loc-city-${i}" type="text"
                     value="${esc(loc.city || '')}" placeholder="如：Frankfurt">
              <div class="loc-error" id="loc-city-err-${i}"></div>
            </div>
            <div class="loc-field">
              <label>国家</label>
              <input class="loc-input" id="loc-country-${i}" type="text"
                     value="${esc(loc.country || '')}" placeholder="如：Germany">
              <div class="loc-error" id="loc-country-err-${i}"></div>
            </div>
          </div>
        </div>
        <div>
          <div class="pref-section-label">求职要求描述</div>
          <p class="pref-desc">该地区的目标职位方向，AI 据此判断是否推送</p>
          <textarea class="pref-textarea" id="pref-loc-${i}">${esc(loc.user_prompt)}</textarea>
          <div class="jd-learn-row">
            <input type="text" class="jd-url-input" id="jd-url-${i}"
                   placeholder="粘贴 JD 网址，AI 自动提取职位要求…">
            <button class="btn-jd-learn" id="jd-btn-${i}"
                    onclick="learnFromJD(${i}, '${esc(loc.name)}')">🔍 从JD学习</button>
          </div>
          <div class="jd-suggest-box" id="jd-suggest-${i}" style="display:none">
            <div class="jd-suggest-label">✨ AI 从JD中提取的职位要求</div>
            <div class="jd-suggest-text" id="jd-suggest-text-${i}"></div>
            <div class="jd-suggest-actions">
              <button class="btn-jd-apply" onclick="applyJDSuggest(${i}, 'replace')">替换</button>
              <button class="btn-jd-merge" onclick="applyJDSuggest(${i}, 'merge')">追加</button>
              <button class="btn-jd-dismiss" onclick="dismissJDSuggest(${i})">忽略</button>
            </div>
          </div>
        </div>
        <div class="bl-section">
          <div class="pref-section-label">🚫 该地区黑名单</div>
          <p class="pref-desc">仅对「${esc(loc.name)}」生效，每行一条</p>
          <div class="bl-row">
            <div class="bl-col">
              <label>屏蔽公司</label>
              <textarea class="bl-textarea" id="pref-bl-companies-${i}">${esc((bl.companies||[]).join('\n'))}</textarea>
              <div class="bl-hint">公司名称含该词即屏蔽</div>
            </div>
            <div class="bl-col">
              <label>屏蔽关键词</label>
              <textarea class="bl-textarea" id="pref-bl-keywords-${i}">${esc((bl.keywords||[]).join('\n'))}</textarea>
              <div class="bl-hint">职位标题含该词即屏蔽</div>
            </div>
          </div>
        </div>
      </div>
    </div>`;
  });
  container.innerHTML = html;
}

function _parseLines(id) {
  const el = document.getElementById(id);
  if (!el) return [];
  return el.value.split('\n').map(s => s.trim()).filter(Boolean);
}

function _validateLocInput(i) {
  const city = (document.getElementById('loc-city-' + i)?.value || '').trim();
  const country = (document.getElementById('loc-country-' + i)?.value || '').trim();
  const cityErr = document.getElementById('loc-city-err-' + i);
  const countryErr = document.getElementById('loc-country-err-' + i);
  const cityInput = document.getElementById('loc-city-' + i);
  const countryInput = document.getElementById('loc-country-' + i);
  let valid = true;

  // Reset
  [cityErr, countryErr].forEach(el => { if (el) el.textContent = ''; });
  [cityInput, countryInput].forEach(el => { if (el) el.classList.remove('error'); });

  if (!city) {
    if (cityErr) cityErr.textContent = '城市不能为空';
    if (cityInput) cityInput.classList.add('error');
    valid = false;
  } else if (city.length < 2) {
    if (cityErr) cityErr.textContent = '城市名称太短';
    if (cityInput) cityInput.classList.add('error');
    valid = false;
  } else if (/\d/.test(city)) {
    if (cityErr) cityErr.textContent = '城市名不应包含数字';
    if (cityInput) cityInput.classList.add('error');
    valid = false;
  } else if (city.includes(',')) {
    if (cityErr) cityErr.textContent = '城市和国家请分开填写，不要用逗号';
    if (cityInput) cityInput.classList.add('error');
    valid = false;
  }

  if (!country) {
    if (countryErr) countryErr.textContent = '国家不能为空';
    if (countryInput) countryInput.classList.add('error');
    valid = false;
  } else if (country.length < 2) {
    if (countryErr) countryErr.textContent = '国家名称太短';
    if (countryInput) countryInput.classList.add('error');
    valid = false;
  } else if (/\d/.test(country)) {
    if (countryErr) countryErr.textContent = '国家名不应包含数字';
    if (countryInput) countryInput.classList.add('error');
    valid = false;
  } else if (country.includes(',')) {
    if (countryErr) countryErr.textContent = '城市和国家请分开填写，不要用逗号';
    if (countryInput) countryInput.classList.add('error');
    valid = false;
  }

  return valid;
}

async function savePreferences() {
  if (!preferencesData) return;
  const hint = document.getElementById('pref-save-hint');

  // Frontend validation
  let allValid = true;
  (preferencesData.locations || []).forEach((_, i) => {
    if (!_validateLocInput(i)) allValid = false;
  });
  if (!allValid) {
    hint.textContent = '⚠ 请修正地区填写错误后再保存';
    hint.style.color = '#e74c3c';
    setTimeout(() => { hint.textContent = ''; hint.style.color = ''; }, 4000);
    return;
  }

  const locations = (preferencesData.locations || []).map((loc, i) => ({
    name: loc.name,
    city: (document.getElementById('loc-city-' + i)?.value || '').trim(),
    country: (document.getElementById('loc-country-' + i)?.value || '').trim(),
    user_prompt: document.getElementById('pref-loc-' + i).value,
    blacklist: {
      companies: _parseLines('pref-bl-companies-' + i),
      keywords:  _parseLines('pref-bl-keywords-' + i),
    }
  }));
  const global_prompt = document.getElementById('pref-global').value;
  const global_blacklist = {
    companies: _parseLines('pref-gbl-companies'),
    keywords:  _parseLines('pref-gbl-keywords'),
  };
  const res = await fetch('/api/preferences', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({locations, global_prompt, global_blacklist})
  });
  const data = await res.json();
  if (data.ok) {
    hint.textContent = '✓ 已保存';
    hint.style.color = '';
  } else {
    const msg = (data.errors || ['保存失败']).join('；');
    hint.textContent = '⚠ ' + msg;
    hint.style.color = '#e74c3c';
  }
  setTimeout(() => { hint.textContent = ''; hint.style.color = ''; }, 5000);
}

async function optimizePreferences() {
  if (!preferencesData) return;
  const btn = document.getElementById('btn-optimize');
  btn.textContent = '分析中…';
  btn.disabled = true;
  const locations = (preferencesData.locations || []).map((loc, i) => ({
    name: loc.name,
    city: (document.getElementById('loc-city-' + i)?.value || '').trim(),
    country: (document.getElementById('loc-country-' + i)?.value || '').trim(),
    user_prompt: document.getElementById('pref-loc-' + i).value,
    blacklist: {
      companies: _parseLines('pref-bl-companies-' + i),
      keywords:  _parseLines('pref-bl-keywords-' + i),
    }
  }));
  const global_prompt = document.getElementById('pref-global').value;
  try {
    const res = await fetch('/api/preferences/optimize', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({locations: locations, global_prompt: global_prompt})
    });
    const data = await res.json();
    if (data.ok) {
      optimizedData = data.result;
      showOptimized(data.result);
    } else {
      alert('优化失败：' + (data.error || '未知错误'));
    }
  } catch(e) {
    alert('请求失败：' + e.message);
  } finally {
    btn.textContent = '✨ AI分析优化';
    btn.disabled = false;
  }
}

function showOptimized(result) {
  const panel = document.getElementById('optimize-panel');
  const content = document.getElementById('optimize-content');
  let html = '';
  if (result.explanation) {
    html += `<p style="font-size:13px;color:#555;margin-bottom:12px">${esc(result.explanation)}</p>`;
  }
  html += `<div class="optimize-item"><label>全局偏好</label>
    <textarea class="pref-textarea" id="opt-global" style="min-height:60px">${esc(result.global_prompt || '')}</textarea></div>`;
  (result.locations || []).forEach((loc, i) => {
    html += `<div class="optimize-item"><label>${esc(loc.name)}</label>
      <textarea class="pref-textarea" id="opt-loc-${i}">${esc(loc.user_prompt || '')}</textarea></div>`;
  });
  content.innerHTML = html;
  panel.classList.add('visible');
}

function applyOptimized() {
  if (!optimizedData) return;
  if (optimizedData.global_prompt) {
    document.getElementById('pref-global').value = optimizedData.global_prompt;
  }
  (optimizedData.locations || []).forEach((loc, i) => {
    const el = document.getElementById('pref-loc-' + i);
    if (el && loc.user_prompt) el.value = loc.user_prompt;
  });
  document.getElementById('optimize-panel').classList.remove('visible');
  document.getElementById('pref-save-hint').textContent = '建议已应用，点击保存生效';
}

// ── JD Learn ─────────────────────────────────────────────────
const jdSuggestions = {};

async function learnFromJD(locIndex, locName) {
  const urlInput = document.getElementById('jd-url-' + locIndex);
  const btn = document.getElementById('jd-btn-' + locIndex);
  const url = urlInput.value.trim();
  if (!url) { alert('请先填入 JD 网址'); return; }
  const currentPrompt = document.getElementById('pref-loc-' + locIndex).value;
  btn.textContent = '分析中…';
  btn.disabled = true;
  try {
    const res = await fetch('/api/preferences/learn-from-jd', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: url, location_name: locName, current_prompt: currentPrompt})
    });
    const data = await res.json();
    if (data.ok) {
      jdSuggestions[locIndex] = data.suggested_prompt;
      document.getElementById('jd-suggest-text-' + locIndex).textContent = data.suggested_prompt;
      document.getElementById('jd-suggest-' + locIndex).style.display = '';
    } else {
      alert('学习失败：' + (data.error || '未知错误'));
    }
  } catch(e) {
    alert('请求失败：' + e.message);
  } finally {
    btn.textContent = '🔍 从JD学习';
    btn.disabled = false;
  }
}

function applyJDSuggest(locIndex, mode) {
  const suggested = jdSuggestions[locIndex];
  if (!suggested) return;
  const textarea = document.getElementById('pref-loc-' + locIndex);
  if (mode === 'replace') {
    textarea.value = suggested;
  } else {
    const current = textarea.value.trim();
    textarea.value = current ? current + '\n' + suggested : suggested;
  }
  dismissJDSuggest(locIndex);
}

function dismissJDSuggest(locIndex) {
  document.getElementById('jd-suggest-' + locIndex).style.display = 'none';
  document.getElementById('jd-url-' + locIndex).value = '';
  delete jdSuggestions[locIndex];
}

// Close modal on overlay click
document.getElementById('dislike-modal').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeModal();
});

loadJobs();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    reasons_json = json.dumps(_DISLIKE_REASONS)
    return render_template_string(_HTML, reasons_json=reasons_json)


@app.route("/api/jobs")
def api_jobs():
    company = request.args.get("company", "")
    status = request.args.get("status", "")
    jobs = get_jobs(
        sort_by="created_at",
        sort_dir="desc",
        filter_company=company,
        filter_status=status,
    )
    return jsonify({"jobs": jobs})


@app.route("/api/feedback", methods=["GET"])
def api_feedback_get():
    items = get_feedback_summary()
    reason_map = dict(_DISLIKE_REASONS)
    for item in items:
        item["reason_text"] = reason_map.get(item.get("reason_code", ""), item.get("reason_code", ""))
    return jsonify({"feedback": items})


@app.route("/api/preferences", methods=["GET"])
def api_preferences_get():
    config = _load_config()
    locations = [
        {
            "name": loc["name"],
            "city": loc.get("city", ""),
            "country": loc.get("country", ""),
            "user_prompt": loc.get("user_prompt", ""),
            "blacklist": loc.get("blacklist", {"companies": [], "keywords": []}),
        }
        for loc in config["search"]["locations"]
    ]
    global_prompt = config["matching"]["ai"].get("user_prompt", "")
    global_blacklist = config["search"].get("blacklist", {"companies": [], "keywords": []})
    return jsonify({
        "locations": locations,
        "global_prompt": global_prompt,
        "global_blacklist": global_blacklist,
    })


def _validate_location(city: str, country: str) -> str | None:
    """返回错误信息，None 表示合法。"""
    if not city.strip():
        return "城市不能为空"
    if not country.strip():
        return "国家不能为空"
    if len(city.strip()) < 2:
        return "城市名称太短，请输入完整城市名"
    if len(country.strip()) < 2:
        return "国家名称太短，请输入完整国家名"
    if len(city) > 60 or len(country) > 60:
        return "城市/国家名称过长"
    if re.search(r"\d", city) or re.search(r"\d", country):
        return "城市/国家名称不应包含数字"
    if "," in city or "," in country:
        return "请分别填写城市和国家，不要用逗号合并"
    return None


@app.route("/api/preferences", methods=["POST"])
def api_preferences_post():
    data = request.get_json(force=True)
    config = _load_config()

    # Validate city/country for all locations
    errors = []
    for loc in data.get("locations", []):
        city = loc.get("city", "").strip()
        country = loc.get("country", "").strip()
        err = _validate_location(city, country)
        if err:
            errors.append(f"【{loc.get('name', '?')}】{err}")
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    loc_map = {loc["name"]: loc for loc in data.get("locations", [])}
    for loc in config["search"]["locations"]:
        if loc["name"] in loc_map:
            src = loc_map[loc["name"]]
            city = src.get("city", "").strip()
            country = src.get("country", "").strip()
            loc["city"] = city
            loc["country"] = country
            loc["location"] = f"{city}, {country}"
            loc["user_prompt"] = src.get("user_prompt", loc.get("user_prompt", ""))
            loc["blacklist"] = src.get("blacklist", loc.get("blacklist", {"companies": [], "keywords": []}))
    if "global_prompt" in data:
        config["matching"]["ai"]["user_prompt"] = data["global_prompt"]
    if "global_blacklist" in data:
        config["search"]["blacklist"] = data["global_blacklist"]
    _save_config(config)
    return jsonify({"ok": True})


@app.route("/api/preferences/optimize", methods=["POST"])
def api_preferences_optimize():
    try:
        data = request.get_json(force=True)
        config = _load_config()
        ai_cfg = config["matching"]["ai"]
        provider = ai_cfg.get("provider", "claude")
        api_key = ai_cfg.get("api_key", "")
        model = ai_cfg.get("model", "")
        if not api_key:
            return jsonify({"ok": False, "error": "未配置 AI API Key"}), 400

        feedbacks = get_feedback_summary()
        reason_map = dict(_DISLIKE_REASONS)
        fb_lines = [
            f"- [{reason_map.get(fb.get('reason_code',''), fb.get('reason_code',''))}] "
            f"{fb.get('title','—')} @ {fb.get('company','—')}"
            for fb in feedbacks[:20]
        ]
        feedback_str = "\n".join(fb_lines) if fb_lines else "暂无历史反馈"

        locations = data.get("locations", [])
        global_prompt = data.get("global_prompt", "")
        loc_lines = "\n".join(
            f"【{loc['name']}】{loc.get('user_prompt', '')}" for loc in locations
        )

        system = (
            "你是求职偏好优化助手。根据用户历史反馈（不感兴趣的职位）和当前筛选条件，"
            "给出改进后的筛选条件，使未来推送更精准。\n"
            "要求：保留用户原有目标方向；根据反馈补充明确的排除条件；表达更清晰具体。\n"
            "只返回 JSON，格式：{\"locations\": [{\"name\": \"地区名\", \"user_prompt\": \"改进后内容\"}, ...], "
            "\"global_prompt\": \"改进后全局偏好\", \"explanation\": \"一句话说明主要改进点\"}"
        )
        user_content = (
            f"当前地区筛选条件：\n{loc_lines}\n\n"
            f"当前全局偏好：{global_prompt}\n\n"
            f"用户历史反馈（不感兴趣的职位）：\n{feedback_str}"
        )

        if provider == "claude":
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=model or "claude-haiku-4-5-20251001",
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user_content}],
            )
            response_text = msg.content[0].text
        elif provider in ("openai", "deepseek"):
            from openai import OpenAI
            base_url = "https://api.deepseek.com" if provider == "deepseek" else None
            default_model = "deepseek-chat" if provider == "deepseek" else "gpt-4o-mini"
            kwargs = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = OpenAI(**kwargs)
            resp = client.chat.completions.create(
                model=model or default_model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user_content}],
                max_tokens=1024,
            )
            response_text = resp.choices[0].message.content
        else:
            return jsonify({"ok": False, "error": f"不支持的 provider: {provider}"}), 400

        clean = re.sub(r"```json|```", "", response_text).strip()
        result = json.loads(clean)
        return jsonify({"ok": True, "result": result})

    except Exception as e:
        logger.error(f"优化偏好失败：{e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/preferences/learn-from-jd", methods=["POST"])
def api_learn_from_jd():
    try:
        data = request.get_json(force=True)
        url = data.get("url", "").strip()
        location_name = data.get("location_name", "")
        current_prompt = data.get("current_prompt", "").strip()

        if not url:
            return jsonify({"ok": False, "error": "请提供 JD 网址"}), 400

        try:
            jd_text = _fetch_jd_text(url)
        except Exception as e:
            return jsonify({"ok": False, "error": f"无法抓取页面：{e}（部分招聘网站需要登录）"}), 400

        if len(jd_text) < 80:
            return jsonify({"ok": False, "error": "页面内容太少，无法解析（可能需要登录或页面为动态加载）"}), 400

        config = _load_config()
        ai_cfg = config["matching"]["ai"]
        provider = ai_cfg.get("provider", "claude")
        api_key = ai_cfg.get("api_key", "")
        model = ai_cfg.get("model", "")

        if not api_key:
            return jsonify({"ok": False, "error": "未配置 AI API Key，请先在 config.json 中填写"}), 400

        system = (
            "你是求职偏好提取助手。从招聘页面内容中提取关键信息，"
            "生成简洁的职位要求描述，用于指导 AI 自动筛选类似职位。\n\n"
            "提取要点：目标职位名称/方向、核心技能要求、所属行业/领域、工作地点（如有）。\n"
            "输出要求：2-4句自然语言描述，语言与 JD 主要语言一致（中/英/意等），"
            "不要输出列表或标题，直接输出描述性文字。\n"
            "只输出提取结果，不要任何解释。"
        )
        context = f"地区：{location_name}\n" if location_name else ""
        if current_prompt:
            context += f"用户当前求职描述（供参考，无需重复）：{current_prompt}\n\n"
        user_content = f"{context}以下是招聘页面内容：\n\n{jd_text}"

        from matcher import _call_ai
        suggested = _call_ai(provider, api_key, model, system, user_content).strip()
        return jsonify({"ok": True, "suggested_prompt": suggested})

    except Exception as e:
        logger.error(f"从JD学习失败：{e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/feedback", methods=["POST"])
def api_feedback_post():
    data = request.get_json(force=True)
    job_id = data.get("job_id", "")
    reason_code = data.get("reason_code", "")
    if not job_id or not reason_code:
        return jsonify({"ok": False, "error": "job_id and reason_code required"}), 400
    try:
        save_feedback(job_id, reason_code)
        update_job(job_id, {"status": "disliked"})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def start_web_ui(port: int = 8083) -> None:
    """在后台线程启动 Web UI。"""
    from threading import Thread
    init_db()
    t = Thread(target=lambda: app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False),
               daemon=True)
    t.start()
    logger.info(f"Web UI 已启动：http://localhost:{port}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
    print("Web UI 启动：http://localhost:8083")
    app.run(host="0.0.0.0", port=8083, debug=True)
