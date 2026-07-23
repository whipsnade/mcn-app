# 收藏功能重建 + 活动评估输入化 + 会话默认建议 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 圈选/推荐达人可收藏并跨登录持久化；活动评估改「活动名+达人名（多值）」输入 + 模型小循环查数评估；新会话 4 条默认建议且全部建议点击改为填入输入框。

**Architecture:** 收藏扩展现有 `user_kol_favorites` 表（迁移 0021，platform+kol_uid 新路径与旧 kol_id 路径并存）；评估复用 quick 模型小循环（新 feature `campaign_evaluate`）；前端星标 + 输入区重写 + ChatArea 点击行为统一。

**Tech Stack:** FastAPI + SQLAlchemy Async + Alembic；React 19 + TS + Vitest。

**Spec:** `docs/superpowers/specs/2026-07-22-favorites-evaluate-suggestions-design.md`（评审 Approved）

**关键背景（零上下文必读）：**

- 迁移 0020 的 revision id 是 `0020_kol_selection_session_rpts`（31 字符，alembic version_num 上限 32）——0021 的 down_revision 必须用它；0021 自身 revision 也要 ≤32 字符。
- `backend/tests/test_phase2_migrations.py` 有 head 断言，新迁移要同步更新。
- 评审建议已采纳：① `run_quick_feature`（quick/agent.py:228）system prompt 硬编码 `QUICK_AGENT_PROMPT`，campaign_evaluate 需要给它加 system prompt 参数（本计划选此方案），且 `quick_feature_tool_names`、`_OUTPUT_CONTRACTS`、`validate_feature_result` 都要加 campaign_evaluate 分支；② 前端 favorites 列表由 **App 层统一拉取下发**（避免每个卡片组件各自请求），`favoritesRefreshKey` 是新 state（现 App.tsx:191 传的是常量 0）；③ 空白会话判定直接用 `session.messages.length === 0`。
- 后端验证：`cd backend && .venv/bin/ruff check app tests && .venv/bin/pytest -q`；前端：`npm run test && npm run lint && npm run build`。
- 预存失败：`tests/integration/test_real_providers.py::test_real_tencent_adapter_uses_confirmed_model`（本地 k3 模型名硬断言），所有任务都不动它。

---

### Task 1: 迁移 0021 + 收藏后端新路径

**Files:**
- Create: `backend/migrations/versions/0021_favorite_platform_uid.py`
- Modify: `backend/app/reporting/models.py`、`backend/app/reporting/schemas.py`、`backend/app/reporting/service.py`、`backend/app/reporting/router.py`
- Test: `backend/tests/test_phase2_migrations.py`、`backend/tests/reporting/test_favorites.py`（若不存在则新建，参照 test_analysis_reports.py 的 auth_client_factory 模式）

- [ ] **Step 1: 迁移 0021（先跑开发库与测试库）**

```python
"""Extend user_kol_favorites with platform/kol_uid identity."""

revision: str = "0021_favorite_platform_uid"   # 26 字符
down_revision: str | None = "0020_kol_selection_session_rpts"


def upgrade() -> None:
    op.add_column("user_kol_favorites", sa.Column("platform", sa.String(32), nullable=True))
    op.add_column("user_kol_favorites", sa.Column("kol_uid", sa.String(128), nullable=True))
    op.add_column("user_kol_favorites",
                  sa.Column("nickname", sa.String(200), nullable=False, server_default=""))
    op.add_column("user_kol_favorites", sa.Column("snapshot_json", sa.JSON(), nullable=True))
    op.alter_column("user_kol_favorites", "kol_id", existing_type=sa.String(36), nullable=True)
    op.create_unique_constraint(
        "uq_user_kol_favorites_user_platform_uid",
        "user_kol_favorites", ["user_id", "platform", "kol_uid"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_user_kol_favorites_user_platform_uid", "user_kol_favorites", type_="unique")
    op.alter_column("user_kol_favorites", "kol_id", existing_type=sa.String(36), nullable=False)
    op.drop_column("user_kol_favorites", "snapshot_json")
    op.drop_column("user_kol_favorites", "nickname")
    op.drop_column("user_kol_favorites", "kol_uid")
    op.drop_column("user_kol_favorites", "platform")
```

```bash
cd backend && .venv/bin/alembic upgrade head
# 测试库：README 中的 APP_ENV=test 迁移命令
```

`test_phase2_migrations.py`：head 断言更新 + 0021 schema 断言（4 个新列、kol_id nullable、新唯一约束存在）。

- [ ] **Step 2: 失败测试（收藏新路径）**

`tests/reporting/test_favorites.py`：
① `POST /favorites {platform, kol_uid, nickname, snapshot}` → 200，列表含 nickname/snapshot，不落 kol_id；
② 同 key 重复 POST → 仍一条（幂等），snapshot 新值非空才更新（传 `{"followers": null, "price": 1000}` 时 followers 旧值不被冲掉、price 更新）；
③ kol_id 与 platform+kol_uid 都不传 → 422；都传 → 422；
④ `DELETE /favorites?platform=&kol_uid=` → 204，再删 → 404；
⑤ 他人不可见/不可删；
⑥ 旧 kol_id 路径创建与删除仍可用（构造 Kol 行）。

- [ ] **Step 3: 实现**

- `models.py` `UserKolFavorite`：加 4 列（与迁移一致）+ 新唯一约束；`kol_id` 改 `Mapped[str | None]`。
- `schemas.py`：`FavoriteCreate` 加 `platform/kol_uid/nickname(默认"")/snapshot: dict|None`，kol_id 改可选；model_validator 校验"kol_id 或 (platform+kol_uid) 必居其一且不两立"。`FavoriteRead` 加 `id/platform/kol_uid/nickname/snapshot`（原字段保留，nickname 改可空兜底）。
- `service.py`：`create_favorite` 分派——platform+kol_uid 走 `INSERT ... ON DUPLICATE KEY UPDATE`（沿用现有写法；nickname/snapshot 仅新值非空才更新；**删除 TaskCandidate 校验分支只针对新路径**，旧 kol_id 路径行为不变）；`delete_favorite_by_key(user_id, platform, kol_uid)`（with_for_update 查行删除，无则 `LookupError("favorite_not_found")`）。
- `router.py`：POST 透传新字段；新增 `DELETE /favorites`（Query: platform, kol_uid → 204/404）——注意路由顺序，别被 `/favorites/{kol_id}` 吃掉；`favorite_read` 装配新字段（新路径行 nickname 读列，不再查 KolSnapshot；旧行维持）。

- [ ] **Step 4: 验证 + commit**

```bash
cd backend && .venv/bin/ruff check app tests && .venv/bin/pytest tests/reporting tests/test_phase2_migrations.py -q
git add backend && git commit -m "feat: 收藏支持 platform+kol_uid 新路径（迁移 0021）"
```

---

### Task 2: 活动评估改 JSON 输入 + 模型小循环

**Files:**
- Modify: `backend/app/quick/router.py`、`backend/app/quick/service.py`、`backend/app/quick/agent.py`、`backend/app/quick/schemas.py`、`backend/app/model/prompts.py`
- Test: `backend/tests/quick/test_evaluate.py`（改写）、`backend/tests/quick/test_quick_agent.py`（参照）

- [ ] **Step 1: 失败测试**

- 端点：`POST /quick/evaluate` JSON `{activity_name, kol_names}` → 200 `{title, analysis_markdown}`（fake 小循环）；kol_names 空/超 20/项超长 → 422；小循环 MCP 调用余额不足 → 409；模型循环失败 → 502。
- 小循环：`campaign_evaluate` feature 的 goal 含活动名与全部达人名；finish 结果按 `{title, analysis_markdown}` 契约校验。

- [ ] **Step 2: 实现**

- `quick/agent.py`：`run_quick_feature` 加可选 `system_prompt` 参数（缺省用 QUICK_AGENT_PROMPT，不影响现有 4 个 feature）；`quick_feature_tool_names`、`_OUTPUT_CONTRACTS`、`validate_feature_result` 加 `campaign_evaluate` 分支（工具白名单：KOL 搜索五平台 + kol_detail + 标签匹配；输出契约为对象型 `{title: str(≤20), analysis_markdown: str(非空)}`）。
- `prompts.py` 新增 `CAMPAIGN_EVALUATE_PROMPT`（注册 PROMPTS）：场景=活动评估，输入含活动名+达人名单+用户行业画像，要求逐个达人查证（搜索→kol_detail 补字段），搜不到的达人在结论中如实说明；评估维度（匹配度/粉丝质量/互动表现/预估成本）由模型自主；输出对象契约；不可信数据与禁编造护栏。
- `quick/service.py`：`QuickService.evaluate_campaign(user, activity_name, kol_names)`：组 goal/scenario → `run_quick_feature(feature="campaign_evaluate", system_prompt=CAMPAIGN_EVALUATE_PROMPT.system, ...)`；计费沿用 QuickCallService。旧 `evaluate`（文件上传）与 `render_upload_table` 删除（确认无其他调用方）。
- `quick/router.py`：`POST /quick/evaluate` 改 JSON body（pydantic：`activity_name: str = Field(min_length=1, max_length=100)`、`kol_names: list[str] = Field(min_length=1, max_length=20)`，项 strip 去空去重后为空 → 422）；错误映射沿用（ValueError→422、InsufficientPointsError→409、ModelAdapterError→502）。
- `quick/schemas.py`：`EvaluateRequest/EvaluateResponse`。

- [ ] **Step 3: 验证 + commit**

```bash
cd backend && .venv/bin/ruff check app tests && .venv/bin/pytest tests/quick -q
git add backend && git commit -m "feat: 活动评估改达人名单输入 + 模型小循环查数"
```

---

### Task 3: 前端收藏（星标 ×2 + FavoritesPanel + App 接线）

**Files:**
- Modify: `src/api/favorites.ts`、`src/api/contracts.ts`、`src/components/UniversalReport.tsx`、`src/components/KolRecommendPanel.tsx`、`src/components/FavoritesPanel.tsx`、`src/App.tsx`
- Test: 各组件测试 + `src/api/favorites.test.ts`（如需新建）

- [ ] **Step 1: API 与类型（测试先行）**

`contracts.ts` `ApiFavorite` 加 `id/platform/kol_uid/nickname/snapshot`；`favorites.ts`：
- `createFavorite(input: {platform, kolUid, nickname?, snapshot?})` → POST `{platform, kol_uid, nickname, snapshot}`；
- `deleteFavoriteByKey(platform, kolUid)` → `DELETE /favorites?platform=..&kol_uid=..`；
- 旧 `createFavorite({kol_id})`/`deleteFavorite(kolId)` 保留（签名冲突时用联合类型或新函数名 `createFavoriteByKey`，选清晰的）。

- [ ] **Step 2: App 层 favorites 统一下发**

App.tsx：`favorites` state + `favoritesRefreshKey` state + `refreshFavorites()`（listFavorites → setState + key+1）；`useEffect` 初始与 key 变化拉取；下发给 UniversalReport、KolRecommendPanel、FavoritesPanel（替换现 FavoritesPanel 自拉模式，props 改受控）。登出/会话切换不重置（收藏是用户级）。

- [ ] **Step 3: 星标组件与两处接入（测试先行）**

小组件 `FavoriteStar({active, busy, onToggle})`（Star 图标，active 实心 amber，aria-label「收藏/取消收藏」）。
- UniversalReport 圈选达人卡片：每行右侧加星标；`active = favorites.some(f => f.platform === item.platform && f.kol_uid === item.kol_uid)`；点击 → active 则 deleteFavoriteByKey 否则 createFavorite（snapshot：`{followers, rating, stars, engagement_rate, quoted_price_cny, city, profile_url}` 防御取数）；成功后 refreshFavorites。
- KolRecommendPanel 推荐卡片：同上（kol_uid=item.kw_uid；snapshot `{followers: item.fans, price, engagement_rate, city, profile_url: null}`）。
- busy 防连击；失败内联/console 简单处理。

- [ ] **Step 4: FavoritesPanel 改造**

受控 props（favorites/loading/onRefresh/onCountChange）；列表项：nickname + 平台中文名 + 快照（粉丝 formatExposure、报价 ¥ 有则显示）；取消按钮按行类型：`f.kol_uid ? deleteFavoriteByKey : deleteFavorite(kol_id)`；空态文案不变。

- [ ] **Step 5: 验证 + commit**

```bash
npm run test && npm run lint && npm run build
git add src && git commit -m "feat: 圈选/推荐达人星标收藏与已收藏面板快照展示"
```

---

### Task 4: 前端评估输入区重写 + 默认建议与填入输入框 + 文档

**Files:**
- Modify: `src/components/EvaluatePanel.tsx`、`src/api/quick.ts`、`src/components/ChatArea.tsx`
- Modify: `AGENTS.md`、`changelog/2026-07-22.md`
- Test: `src/components/EvaluatePanel.test.tsx`、`src/components/ChatArea.test.tsx`（或现有对应测试文件）

- [ ] **Step 1: quick.ts 改 JSON（测试先行）**

`postEvaluate(input: {activityName: string, kolNames: string[]})` → `request('/api/v1/quick/evaluate', {method: 'POST', body: JSON.stringify({activity_name, kol_names})})`；删除 FormData 版本。错误提示沿用 quickErrorMessage。

- [ ] **Step 2: EvaluatePanel 重写（测试先行）**

- 删除 modal/isModalOpen/file input/validateFile。
- 底部输入区：活动名称 input + 达人名称多值（回车/逗号/失焦添加 chip；chip × 删除；strip 去重；空或超 20 提示）；「开始评估」按钮（activityName 非空且 kolNames ≥1 才可点）。
- loading/结果态（MarkdownBlock + 「重新评估」清空结果保留输入）沿用现有渲染。

- [ ] **Step 3: ChatArea 默认建议 + 点击填入输入框（测试先行）**

- 文件内常量 `DEFAULT_SUGGESTIONS`（4 条，标题/文案按 spec §3）。
- `session.messages.length === 0` 且无 followupStatus 时建议区渲染默认建议（样式同 followup chips）。
- 点击行为三处统一：默认建议、followup chips（现 :316 直接 onSendMessage）、brainstorm chips（:216）→ 改为 `setInputText(suggestion.prompt)` + textarea focus（`textareaRef.current?.focus()`）；不再自动提交。
- 测试：默认建议渲染；三处点击后 inputText 值 = prompt 且未调 onSendMessage。

- [ ] **Step 4: 文档**

- `AGENTS.md`：收藏段落（新路径 platform+kol_uid、迁移 0021）、快捷功能段（评估改达人名单输入 + campaign_evaluate 小循环、计费）、建议行为（点击填入输入框、新会话默认建议）。
- `changelog/2026-07-22.md`：追加本次四个功能（背景/改动/验证/遗留：旧 kol_id 收藏路径保留、render_upload_table 已删、默认建议静态前端常量）。

- [ ] **Step 5: 全量验证 + commit**

```bash
npm run test && npm run lint && npm run build
cd backend && .venv/bin/ruff check app tests && .venv/bin/pytest -q
git add src AGENTS.md changelog && git commit -m "feat: 评估输入区重写 + 默认建议点击填入输入框 + 文档"
```

---

## 风险与备注

- quick 小循环是同步 HTTP：达人多（20 个）时耗时长（每轮决策 10-38s + MCP 30s 超时），前端 loading 文案提示「评估中，可能需要几分钟」。
- 评估走 MCP 计费（每次调用 10 积分）：余额不足 409 前端沿用 quickErrorMessage。
- UAT 同步在所有任务完成后统一进行（含迁移 0021）。
