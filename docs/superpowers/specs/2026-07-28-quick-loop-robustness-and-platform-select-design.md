# quick 小循环健壮性修复 + 达人推荐平台多选 设计

日期：2026-07-28
状态：已确认（用户要求修复失败并增加平台多选）

## 背景与事故核实

### 本地：参数信封导致 502（系统性）

`GET /quick/kol-recommendations` 连续 502。实证（model_prompt_logs）：模型连续两轮把
`datatap.social.grow.kol.match.mentions.tag.v1` 的参数包成 `{"request": {keywords,
platform, mentionsTagType}}`，而该工具注册 schema 是平铺必填
（`mcp_tool_catalog.input_schema_json`，required=[platform,mentionsTagType,keywords]）。
**实际故障路径**（spec 评审核实）：`quick/agent.py` 的 `resolve_agent_call` 先
`_prune_arguments` 剔除未声明的 `request` 键 → `validate_input` 因缺 required 字段抛
`McpValidationError` → 包装为 `PlanValidationError("INVALID_TOOL_ARGUMENTS")` → 进入
`agent.py:306-322` 的 invalid_streak 护栏，连续 2 次即 `invalid_decision` → 502
（`QuickCallFailedError("input_validation_error")` 的 service 路径根本走不到，
`quick_mcp_calls` 无记录与此吻合）。
同时段爆贴 502 是 DataTap 上游超时（`possibly_sent_timeout`），属供应商抖动。

### UAT：模型调用超时 502

`openai.APITimeoutError`（model_timeout_seconds=180s）→ `MODEL_TIMEOUT` → 502，
发生在 quick 小循环模型调用。注意 `tencent_plan.py:683` 把超时映射为
`retryable=False`，重试必须按 `code == "MODEL_TIMEOUT"` 特判。

## 修复方案

### 1. INVALID_TOOL_ARGUMENTS 回喂增强与计数放宽（`quick/agent.py`）

- 信封/参数错误的实际捕获点是 `agent.py:306-322` 的
  `PlanValidationError("INVALID_TOOL_ARGUMENTS")` 分支。回喂增强：从
  `error.__cause__`（McpValidationError）取具体缺失/多余字段（截断 300 字符），连同
  「arguments 必须是该工具 schema 的平铺顶层字段，禁止包 request 信封」的提示回喂
  模型，继续下一轮。
- **计数放宽**：现有 invalid_streak 护栏是「连续 2 次失败即终止」，本修复将
  INVALID_TOOL_ARGUMENTS 的连续失败上限放宽到 3（即给模型 2 次修正机会，第 3 次仍错
  才失败）——是对既有计数的放宽，不新增独立计数器；其余
  PlanValidationError（TOOL_NOT_ALLOWED/渠道/缺名）维持上限 2 不变。
- 其余 `QuickCallFailedError`（tool_not_enabled/上游错误）维持现状直接失败。

### 2. request 信封防御性解包（`quick/agent.py`，决策解析后、resolve_agent_call 前）

- 在 quick 小循环本地（**不动共享的 `resolve_agent_call`**）：当 decision.arguments
  只有一个 `request` 键、其值是 dict、且目标工具 schema 不含 `request` 属性时，先自动
  解包为内层 dict 再交给 `resolve_agent_call`；解包后仍不合法则按 INVALID_TOOL_ARGUMENTS
  进入 §1 回喂。注释说明这是模型常见信封习惯的兼容归一化。

### 3. MODEL_TIMEOUT 重试一次（`quick/agent.py`）

- 小循环的模型调用遇 `ModelAdapterError` 且 `error.code == "MODEL_TIMEOUT"` 时重试
  一次（该码 `retryable=False`，必须按 code 特判，不看 retryable 标志）；仍失败按
  现状上抛 502。adapter 层对 retryable 错误已有内部重试，不重复处理。

### 4. 达人推荐平台多选（前端）

- 后端无需改动：`GET /quick/kol-recommendations` 已支持 `platforms` 查询参数。
- 前端新建五平台常量与中文 label（xiaohongshu/douyin/bilibili/weibo/wechat——现有
  `ApiQuickPlatform` 只有 2 个，不可用）。
- `KolRecommendPanel`：预算滑动条上方加平台多选 chips（复用 brainstorm 多选交互：
  可切换选中态，aria-pressed，选中高亮），**默认全选**（有意为之：与后端缺省
  「用户启用渠道 ∩ 五平台」不同，以多选 UI 为准）。
- 选择存入 `kolRecommend` 缓存 entry（`platforms: string[]`，切 Tab 保留）；
  `queryKolRecommendations(budget, platforms)` 透传到 `getKolRecommendations`
  （该 API 已支持 `platforms?: string[]`）；全不选时禁用查询按钮。
- 重新查询触发 key：budget 或 platforms 变化都触发（`budget !== queriedBudget ||
  排序后 platforms.join() !== queriedPlatforms`——queriedPlatforms 一并入缓存 entry；
  比较前排序避免 chips 切换顺序造成假差异）。

### 不做的事（YAGNI）

- 不改爆贴/活动评估的工具循环（同架构收益自然共享 §1/§3 的 quick/agent 层修复）。
- 不动 DataTap 上游超时（供应商问题，靠 possibly_sent_timeout 的既有语义）。
- 平台多选不做「记住上次选择」的跨用户持久化（缓存按 userId 隔离即可）。

## 测试策略

- quick/agent：INVALID_TOOL_ARGUMENTS 回喂内容（含缺失字段与信封提示）、第 3 次才失败、
  第 2 次修正成功继续；request 信封解包（合法内层正常执行、非法内层仍回喂）；
  MODEL_TIMEOUT 重试一次成功/再失败按原样上抛。
- KolRecommendPanel：多选切换、入缓存、切 Tab 保留、查询带 platforms 参数、
  全不选禁用、改平台触发重新查询（排序后比较）。

## 遗留事项

- 模型为何突然开始包 request 信封（prompt 学习案例里是否混入了带信封的 exemplar）
  待观测；若高频复发，需在 exemplars 投影层剔除信封形态。
