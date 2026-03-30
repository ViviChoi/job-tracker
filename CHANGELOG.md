# Changelog

## [2026-03-30] 第二批

### Bug Fixes

**1. 匹配模式字符串不一致 (`notifier.py`, `templates/setup.html`)**
- `notifier.py` 检查 `mode == "keyword"`，但全库统一用 `"keywords"`，导致关键词模式下推送消息永远显示 AI 格式
- `setup.html` radio value 也是 `"keyword"`，导致 `main.py` 里 `mode == "keywords"` 的简历检查永远跳过
- 修复：统一改为 `"keywords"`，`populate()` 加旧值兼容

**2. 非匹配职位在定时模式下被推送 (`storage.py`, `main.py`)**
- `save_job` 对所有职位固定写 `notified=0`，`get_pending_jobs` 拉取全量未通知记录
- 定时模式下会把未匹配职位也打包发给用户
- 修复：`save_job` 新增 `notified` 参数，非匹配职位保存时传 `notified=1`

**3. 免打扰时段结束后积压职位丢失 (`main.py`)**
- 安静时段内匹配的职位暂存 DB，但下一轮 `run_cycle` 进来时 `is_seen()` 已返回 True，职位永远不会被再次处理
- 修复：进入推送逻辑前先 flush `get_pending_jobs()` 中未通知的积压职位

**4. `_load_resume` 锁范围错误导致竞态 (`outreach_gen.py`)**
- 锁只保护了 `if _RESUME_CACHE is not None` 检查，锁释放后才进行文件加载和赋值
- 两个线程同时进入后可能互相覆盖，失败线程写入空字符串覆盖成功线程的结果
- 修复：将整个加载 + 赋值过程移入锁内

**5. HTML 解析器嵌套跳过标签 bug (`web_ui.py`)**
- `_Extractor` 用布尔值 `_skip` 标记跳过，遇到 `<script>` 嵌套在 `<nav>` 内时，`</script>` 会提前把 `_skip` 置 False
- 导致 JS 代码混入抓取的 JD 文本传给 AI
- 修复：改用深度计数器 `_skip_depth`

**6. 别名缓存多线程写竞态 (`alias_learner.py`)**
- 多个关键词并发触发别名学习时，同时读写 `alias_cache.json`，互相覆盖对方的结果
- 修复：新增 `_CACHE_LOCK`，读 + 修改 + 写全程加锁

**7. 保存配置丢失非 UI 管理字段 (`setup.py`)**
- `update_config` 直接用前端数据整体覆盖 config 文件
- `setup.html` 的 `collectConfig` 不包含 `outreach`、`resume_bot`、`web_ui` 字段，每次保存这些配置都会被清空
- 修复：保存前先将旧 config 中这三个 key 合并进新 config

**8. `storage.mode` 保存后丢失 (`templates/setup.html`)**
- `collectConfig` 构建 storage 对象时未包含 `mode` 字段
- 导致保存后触发虚假的「存储方式切换」警告，且真实 mode 丢失
- 修复：补入 `mode: config.storage?.mode || 'local'`

---

## [2026-03-30] 第一批

### New Features

**1. 地区配置拆分为城市 + 国家双输入框 (`templates/setup.html`)**
- 原单输入框「城市/地区」改为并排两个独立输入框：城市 / 国家
- 系统自动拼接为 `"City, Country"` 传给 jobspy，彻底消除城市名地区歧义
- 保存时校验：不能为空、不能含数字、不能含逗号、长度不低于 2 字符
- 兼容旧配置：已有 `location` 字段自动按逗号拆分回填，迁移无感知

**2. 新增搜索地区**
- 新增 **Berlin, Germany**（关键词与法兰克福一致）
- 新增 **Vienna, Austria**（关键词与米兰一致）

### Bug Fixes

**3. `config.json` 缺 city/country 字段**
- 启动时自动从 `location` 字符串拆分回填 city/country
- 修正米兰 location 为 `"Milano, Italy"`（原为 `"Milano"`，导致 jobspy 解析为美国德州）

**4. AI 别名学习 location 参数永远为空 (`templates/setup.html`)**
- `_syncLocAliasesWithKeywords` 读取了已废弃的 DOM ID `loc-${i}-location`
- 修复为读 `loc-${i}-city` + `loc-${i}-country` 拼合后传入

**5. dislike 状态刷新后丢失 (`web_ui.py`)**
- `api_feedback_post` 只写 `user_feedback` 表，未更新 `jobs.status`
- 修复：补加 `update_job(job_id, {"status": "disliked"})`，状态持久化

---

## [2026-03-27]

### Bug Fixes

**1. 不相关职位被筛选进来 (`matcher.py`)**
- AI 提示词加强了「职位大类差距极大」的判断，并明确列举不相关职种示例
- 新增**公民身份/签证限制**为独立硬门槛（第4条），覆盖中/英/意/德语多种表述
  - `no visa sponsorship`、`must be [国家] citizen`、`nationals only` 等均触发拒绝
- 新增用户历史反馈注入：将用户近期 👎 反馈的职位类型传入 AI 上下文，引导避免类似推送

**2. HR 联系人不是所推送公司的 (`hr_finder.py`)**
- 新增 `_is_company_match()` 函数：验证搜索结果中联系人 snippet/title 是否包含目标公司名
- 三级 fallback 策略：
  1. 优先返回「HR 信号 + 公司名匹配」的联系人
  2. 次选「HR 信号匹配」（同时记录警告日志）
  3. 再次选「公司名匹配」
  4. 最后兜底返回所有结果
- `find_hr_contacts()` 新增 `max_results` 截断，避免返回过多无关结果

### New Features

**3. 减少推荐功能 — Telegram Bot**
- 每条职位推送卡片新增 **👎 减少推荐** 按钮（与 ✅ 感兴趣并排）
- 点击后展示 6 种原因选项：
  - 工作内容不相关 / 地点不符合 / 需要公民身份/签证
  - 级别要求太高 / 行业/职种方向不对 / 其他原因
- 选择原因后自动保存到数据库，按钮变为「👎 已反馈：xxx」
- 反馈数据实时影响后续 AI 匹配（见上方 Fix 1）

**4. Web UI (`web_ui.py`) — http://localhost:8083**
- 职位列表页：支持按公司名搜索、按状态筛选
- 每个职位卡片显示标题、公司、地点、匹配原因、状态
- 「👎」按钮弹出模态框选择减少推荐原因，与 Telegram Bot 共享同一反馈表
- 用户反馈标签页：查看所有历史反馈记录
- 随 `main.py` 启动自动在后台运行（默认端口 8083，可在 config.json 的 `web_ui.port` 修改）

### Database

- `storage.py` 新增 `user_feedback` 表（自动迁移，无需手动操作）：
  ```
  user_feedback (id, job_id, reason_code, reason_text, created_at)
  ```
- 新增 `save_feedback()` 和 `get_feedback_summary()` 函数
