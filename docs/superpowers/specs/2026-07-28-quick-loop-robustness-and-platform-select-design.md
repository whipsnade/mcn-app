# quick 小循环健壮性修复 + 达人推荐平台多选 设计

日期：2026-07-28
状态：已确认（用户要求修复失败并增加平台多选）

## 背景与事故核实

### 本地：参数信封导致 502（系统性）

`GET /quick/kol-recommendations` 连续 502。实证（model_prompt_logs）：模型连续两轮把
`datatap.social.grow.kol.match.mentions.tag.v1` 的参数包成 `{"request": {keywords,
platform, mentionsTagType}}`，而该工具注册 schema 是平铺必填
（`mcp_tool_catalog.input_schema_json`，required=[platform,mentionsTagType,keywords]）
→ `validate_input` 失败 → `QuickCallFailedError("input_validation_error")`（在计费
留痕前抛出，`quick_mcp_calls` 无记录）→ 小循环 2 次同样错误后 502。
同时段爆贴 502 是 DataTap 上游超时（`possibly_sent_timeout`），属供应商抖动。

### UAT：模型调用超时 502

`openai.APITimeoutError`（model_timeout_seconds=180s）→ `MODEL_TIMEOUT` → 502，
发生在 quick 小循环模型调用，无重试。

## 修复方案

### 1. input_validation_error 改为可恢复决策错误（`quick/agent.py`）

- `QuickCallFailedError("input_validation_error")` 不再直接终结小循环：把校验失败的
  具体原因（缺失字段/多余字段，来自 McpValidationError 的 message 截断 300 字符）
  连同「arguments 必须是该工具 schema 的平铺顶层字段，禁止包 request 信封」的提示
  回喂模型，继续下一轮。
- **连续修正上限 2 次**（独立计数）；第 3 次仍错才按现状失败（502）。
- 其余 QuickCallFailedError（tool_not_enabled/上游错误）维持现状直接失败。

### 2. request 信封防御性解包（`quick/service.py` 的 `call_tool` 校验前）

- 当 `arguments` 只有一个 `request` 键、其值是 dict、且该工具的 schema 不含
  `request` 属性时，自动解包为内层 dict 再进 `validate_input`；解包后仍不合法则按
  正常校验错误处理（进入 §1 回喂）。注释说明这是模型常见信封习惯的兼容归一化。

### 3. MODEL_TIMEOUT 重试一次（`quick/agent.py`）

- 小循环的模型调用遇 `ModelAdapterError` 且 `retryable=True`（含 MODEL_TIMEOUT）时，
  最多重试一次；仍失败按现状上抛 502。

### 4. 达人推荐平台多选（前端）

- 后端无需改动：`GET /quick/kol-recommendations` 已支持 `platforms` 查询参数
  （`_parse_platforms` 按逗号分隔、校验属于 KOL_SEARCH_TOOLS 平台集）。
- `KolRecommendPanel`：预算滑动条上方加平台多选 chips（复用 brainstorm 多选交互：
  可切换选中态，aria-pressed，选中高亮）；平台集合 = KOL_SEARCH_TOOLS 支持的
  平台（读前端 contracts/常量，全部默认选中）。
- 选择存入 `kolRecommend` 缓存 entry（`platforms: string[]`，切 Tab 保留）；
  `getKolRecommendations(budget, platforms)` 传 `platforms=逗号拼接`；全不选时禁用
  查询按钮。
- 后端 platforms 变化触发重新查询（纳入防抖 key：`budget !== queriedBudget ||
  platforms.join() !== queriedPlatforms`——queriedPlatforms 一并入缓存 entry）。

### 不做的事（YAGNI）

- 不改爆贴/活动评估的工具循环（同架构收益自然共享 §1/§3 的 quick/agent 层修复）。
- 不动 DataTap 上游超时（供应商问题，靠 possibly_sent_timeout 的既有语义）。
- 平台多选不做「记住上次选择」的跨用户持久化（缓存按 userId 隔离即可）。

## 测试策略

- quick/agent：input_validation_error 回喂内容（含信封提示）、2 次内修正成功继续、
  第 3 次失败；MODEL_TIMEOUT 重试一次成功/再失败。
- quick/service：request 信封解包（合法内层通过、非法内层回报校验错误）。
- KolRecommendPanel：多选切换、入缓存、切 Tab 保留、查询带 platforms 参数、
  全不选禁用、改平台触发重新查询。

## 遗留事项

- 模型为何突然开始包 request 信封（prompt 学习案例里是否混入了带信封的 exemplar）
  待观测；若高频复发，需在 exemplars 投影层剔除信封形态。
