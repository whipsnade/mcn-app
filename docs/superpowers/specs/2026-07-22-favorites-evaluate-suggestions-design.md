# 收藏功能重建 + 活动评估输入化 + 会话默认建议 — 设计文档

日期：2026-07-22（晚间追加）
状态：已与用户确认

## 背景与目标

四个新需求（用户沟通后确认）：

1. 右侧「圈选达人」列表每行加收藏图标；收藏后出现在会话 tab「已收藏」列表，落库持久化，每次登录可见，可在已收藏界面取消。
2. 会话 tab「达人推荐」结果卡片同样可收藏。
3. 会话 tab「活动评估」取消文件上传，改为底部输入框：活动名称 + 达人名称（多值）。
4. 会话底部建议区：新会话提供 4 条系统默认建议；点击建议不直接提交，而是填入输入框。

已确认决策：

- 活动评估数据：**模型小循环查数**（quick agent 按达人名搜索/拉详情，每次 MCP 调用 10 积分），有真实数据再评估。
- 收藏模型：**扩展现有表**（迁移加列，kol_id 改可空，旧数据不动）。
- 建议点击行为：**全部建议统一填入输入框**（默认建议、followup 建议、brainstorm 选项 chips）。

## 现状要点（探索结论）

- 收藏：`user_kol_favorites`（迁移 0003）硬绑旧 `kols` 表（不再写入），`create_favorite` 带 source_task_id 走 TaskCandidate 死校验恒 404；列表经 join Kol + KolSnapshot 取昵称（N+1）。前端 FavoritesPanel 只显示 nickname+platform。端点：`GET/POST /favorites`、`DELETE /favorites/{kol_id}`（reporting/router.py）。
- 达人推荐：`KolRecommendationItem{platform, kw_uid, nickname?, fans?, price?, engagement_rate?, score?, city?, tags[]}`（quick/schemas.py）。
- 活动评估：`POST /quick/evaluate` 收 multipart 文件 → render_upload_table → EVALUATE_PROMPT 纯模型（0 积分）；前端 EvaluatePanel 为 modal + 文件选择，无输入框。
- 建议：followup 建议存 assistant 消息 metadata（任务终态生成，空白会话无）；前端 ChatArea:316 点击直接 `onSendMessage`；brainstorm chips 同；输入框是受控 `inputText`（ChatArea.tsx:46），无外部填入口。
- 收藏标的：圈选达人（session_kol_selections 行：platform/kol_uid/nickname/followers/score_json/fields_json）与推荐达人（kw_uid/platform/nickname/fans/price/engagement_rate/city）。

## 设计

### 1. 收藏（圈选达人 + 达人推荐）

**迁移 0021**（down_revision=`0020_kol_selection_session_rpts`——注意 revision 短名）：
- `user_kol_favorites` 加列：`platform` String(32) 可空、`kol_uid` String(128) 可空、`nickname` String(200) default ""、`snapshot_json` JSON 可空（followers/price/engagement_rate/city/rating/stars/profile_url 等快照）。
- `kol_id` 改可空；新增唯一约束 `uq_user_kol_favorites_user_platform_uid(user_id, platform, kol_uid)`（MySQL 唯一索引允许多个 NULL，旧行不受影响）；旧 `(user_id, kol_id)` 保留。

**后端**（reporting/service.py + router.py + schemas.py）：
- `FavoriteCreate` 扩展：`kol_id` 可空 + 新增 `platform/kol_uid/nickname/snapshot` 可选；校验：kol_id 或 (platform+kol_uid) 必居其一，兼有则 422。
- `create_favorite` 新路径：platform+kol_uid → 幂等 upsert（`INSERT ... ON DUPLICATE KEY UPDATE`，沿用现有模式；snapshot 新值非空才更新）；**不再走 TaskCandidate 校验**；旧 kol_id 路径保留。
- `FavoriteRead` 扩展：`id`（收藏行 id）、`platform/kol_uid/nickname/snapshot`；新路径行 nickname 直接读列（不再 join KolSnapshot，消除 N+1）；旧行维持 join 行为。
- 删除：新增 `DELETE /favorites?platform=&kol_uid=`（新路径）；旧 `DELETE /favorites/{kol_id}` 保留。

**前端**：
- `src/api/favorites.ts`：`createFavorite` 支持新载荷；`deleteFavoriteByKey(platform, kolUid)`；类型同步。
- UniversalReport 圈选达人卡片与 KolRecommendPanel 推荐卡片：每行星标按钮（Star，收藏后实心）。点击调 createFavorite（圈选：platform/kol_uid/nickname + snapshot{followers, rating, stars, engagement_rate, quoted_price_cny, city, profile_url}；推荐：platform/kw_uid→kol_uid/nickname + snapshot{fans→followers, price, engagement_rate, city}）。已收藏判定：拉 favorites 列表按 (platform, kol_uid) 匹配；再点已收藏 = 取消。
- FavoritesPanel：列表项显示 nickname + 平台 + 快照（粉丝/报价有则显示）；取消收藏按行类型走对应删除端点。
- 跨面板刷新：App 层 favoritesRefreshKey 状态，任何收藏/取消操作递增，FavoritesPanel 与各卡片依赖它重拉（沿用现有 refreshKey 模式）。

### 2. 活动评估改输入模式

**后端**：
- `POST /quick/evaluate` 改 JSON body：`{activity_name: str(1-100), kol_names: list[str](1-20 项，每项 1-64)}`；删除 multipart 文件上传路径与 `render_upload_table`（若无其他调用方）。
- 走 quick 模型小循环（quick/agent.py 的 `run_quick_feature` 模式，新 feature key `campaign_evaluate`）：system prompt（新 `CAMPAIGN_EVALUATE_PROMPT`）说明活动与达人名单，模型逐个达人搜索/拉详情后 finish；护栏沿用（白名单/Schema 校验/8 轮上限/QuickCallService 计费 10 积分/次/quick_mcp_calls 留痕/余额不足 409 INSUFFICIENT_POINTS）；输出契约 `{title: str(≤20), analysis_markdown: str}` 不变。
- 评估维度交给模型，prompt 只给场景与护栏（与 quick 现有哲学一致）。

**前端**（EvaluatePanel 重写输入区）：
- 删除 modal + 文件 input + validateFile + postEvaluate FormData。
- 底部输入区：活动名称单行输入 + 达人名称多值输入（回车/逗号添加为 chip，chip 可删除，去重，上限 20）+「开始评估」按钮（两栏非空才可点）。
- `postEvaluate` 改为 JSON POST `{activity_name, kol_names}`（走 client.request）；loading / 结果态（MarkdownBlock + 重新评估）沿用；错误提示沿用 quickErrorMessage（409 积分不足、502 模型错误）。
- contracts.ts `ApiQuickEvaluateResult` 不变。

### 3. 默认建议 + 点击填入输入框

- ChatArea 建议区在**空白会话**（无 followupStatus 且无消息）时显示 4 条系统默认建议（前端静态常量 `DEFAULT_SUGGESTIONS`）：
  - 「品牌声量分析」→ `分析某品牌最近3个月在各平台的声量变化和用户情感趋势`
  - 「多品牌对比」→ `对比多个品牌在社交媒体的传播策略和用户反响差异`
  - 「行业趋势分析」→ `分析某行业在社交媒体的讨论热度、用户关注点和发展趋势`
  - 「品牌提及博主圈选」→ `圈选近1个月内容中提及过某品牌的各平台博主，按互动量排序`
- 点击行为统一改为**填入输入框并聚焦 textarea**（`setInputText(prompt)`），不直接提交：默认建议、followup 建议 chips（ChatArea.tsx:316）、brainstorm 选项 chips（ChatArea.tsx:216）三处一致。

### 错误处理

- 收藏重复：幂等成功（与现有一致）。
- 评估：kol_names 空/超上限 422；积分不足 409；模型小循环失败 502；个别达人搜不到由模型在结论中说明（不阻塞整体）。
- 收藏星标的已收藏判定失败（favorites 拉取失败）：星标按未收藏显示，不阻塞列表。

### 测试

- 后端：迁移 0021 schema 断言（test_phase2_migrations 模式）；收藏新路径（创建/幂等/快照合并/按 key 删除/列表返回快照、422 校验）；评估端点（JSON 契约、小循环 fake、422/409/502、计费留痕）。
- 前端：星标收藏/取消/已收藏态；FavoritesPanel 快照展示与删除；EvaluatePanel 多值输入交互与提交；默认建议渲染与三处点击填入输入框。
- AGENTS.md + changelog/2026-07-22.md 同步。

### 不做的事（YAGNI）

- 收藏备注 note 的 UI（保留后端字段）；收藏分组/标签。
- 评估的历史记录持久化（维持现状：结果不存库）。
- 默认建议的后端化/个性化（静态前端常量即可）。
