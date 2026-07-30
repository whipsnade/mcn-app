# 圈选达人详情 BI 弹窗 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户在圈选达人卡片中打开缓存优先的居中 BI 详情弹窗，查看真实 MCP 返回的达人资料、图表、公开主页与 5 条最新热帖。

**Architecture:** 后端为名单版本下的达人详情新增独立缓存表和服务。查询端点先读缓存；缓存未命中或明确刷新时复用 `QuickService.kol_detail` 的真实 MCP 小循环，成功后以归一化 DTO 写入缓存。前端在 `UniversalReport` 的名单卡片上接入 Dialog，使用现有 Recharts 渲染详情 BI，并让缓存、刷新、积分与错误状态在弹窗内自洽。

**Tech Stack:** FastAPI、SQLAlchemy Async、Alembic、pytest、React 19、TypeScript、Vitest、Tailwind CSS 4、Recharts。

## Global Constraints

- 缓存身份固定为 `(selection_set_id, platform, kol_uid)`，不得跨名单版本复用。
- 首次查询和手动刷新使用真实 MCP，并由既有 `QuickCallService` 按实际调用次数结算；缓存命中零积分。
- 不持久化或返回 MCP 原始响应、内部工具参数、密钥或未白名单化字段。
- 热帖最多保存和展示 5 条；原帖链接、达人主页只有非空且公开时才渲染。
- 不修改圈选名单评分、Top20 评分快照、收藏和 Excel 导出行为。
- 未返回的数据必须显示“暂无数据”或模块级缺失提示，不能推测或填充。
- 所有详情缓存读写必须同时校验当前用户、session 与 selection set 归属。

---

## 文件结构

- `backend/migrations/versions/0025_kol_selection_detail_views.py`：创建名单版本级详情缓存表及唯一索引。
- `backend/app/selection/models.py`：新增 `KolSelectionDetailView` ORM 模型。
- `backend/app/selection/detail_views.py`：缓存的安全读写、归一化和基础快照合并。
- `backend/app/selection/service.py`：校验名单身份、读取基础详情、缓存命中/刷新编排。
- `backend/app/selection/router.py`：新增详情读取与查询 API，复用 Quick MCP 计费和现有错误码。
- `backend/tests/selection/test_detail_views.py`：缓存存储、归一化、旧缓存不被失败刷新覆盖。
- `backend/tests/selection/test_kol_selection_detail_endpoints.py`：归属、缓存命中、查询、刷新、余额不足与错误 HTTP 契约。
- `src/api/kolSelection.ts`：详情 API 类型与客户端函数。
- `src/components/KolSelectionDetailDialog.tsx`：居中 Dialog、状态管理、指标与 BI 图表、5 条热帖。
- `src/components/KolSelectionDetailDialog.test.tsx`：Dialog 的缓存/加载/缺失/链接/可访问性渲染测试。
- `src/components/UniversalReport.tsx`：名单卡片可键盘打开，接入 Dialog 与当前名单版本 ID。
- `src/components/UniversalReport.test.tsx`：列表入口、收藏/主页操作不冒泡、关闭后焦点恢复。
- `changelog/2026-07-29.md`：记录功能、积分与缓存口径。

## Task 1: 名单版本级详情缓存模型与安全 DTO

**Files:**

- Create: `backend/migrations/versions/0025_kol_selection_detail_views.py`
- Create: `backend/app/selection/detail_views.py`
- Modify: `backend/app/selection/models.py`
- Test: `backend/tests/selection/test_detail_views.py`

**Interfaces:**

- Consumes: `KolSelectionSet`、`KolSelectionItem`、`KolSelectionDetailSnapshot`。
- Produces: `KolSelectionDetailView`；`DetailViewStore.get()`、`DetailViewStore.upsert()`；`normalize_detail_view_payload()`。

- [ ] **Step 1: 写入缓存覆盖与白名单归一化的失败测试**

  生产变更会使该测试失败：删除唯一身份约束、把 6 条热帖全部写入、或让未白名单的
  `secret/raw_response` 漏进返回 DTO。

  ```python
  async def test_upsert_replaces_one_selection_version_cache_and_limits_safe_posts(db_session):
      store = DetailViewStore(db_session)
      first = await store.upsert(
          selection_set_id="set-1", platform="douyin", kol_uid="uid-1",
          detail={"followers": 120000, "raw_response": {"secret": "never-store"}},
          posts=[{"title": f"帖子{i}", "url": f"https://example.com/{i}"} for i in range(6)],
          points_cost=20, posts_degraded=False,
      )
      second = await store.upsert(
          selection_set_id="set-1", platform="douyin", kol_uid="uid-1",
          detail={"followers": 130000}, posts=[], points_cost=30, posts_degraded=True,
      )

      assert second.id == first.id
      assert second.detail_json == {"followers": 130000}
      assert second.posts_json == []
      assert second.points_cost == 30
      assert second.posts_degraded is True
  ```

- [ ] **Step 2: 运行测试确认失败**

  Run: `cd backend && .venv/bin/pytest -q tests/selection/test_detail_views.py`

  Expected: FAIL，因为 `DetailViewStore` 与 `KolSelectionDetailView` 尚不存在。

- [ ] **Step 3: 创建迁移、模型与窄存储接口**

  创建表字段：`id`、`selection_set_id`（FK CASCADE）、`platform`、`kol_uid`、
  `detail_json`、`posts_json`、`points_cost`、`posts_degraded`、`fetched_at`、
  `created_at`、`updated_at`；建立唯一约束 `uq_kol_detail_view_set_platform_uid` 与按
  `selection_set_id` 的索引。

  ```python
  class DetailViewStore:
      async def get(self, *, selection_set_id: str, platform: str, kol_uid: str) -> KolSelectionDetailView | None:
          return await self._db.scalar(
              select(KolSelectionDetailView).where(
                  KolSelectionDetailView.selection_set_id == selection_set_id,
                  KolSelectionDetailView.platform == platform,
                  KolSelectionDetailView.kol_uid == kol_uid,
              )
          )

      async def upsert(self, *, selection_set_id: str, platform: str, kol_uid: str,
                       detail: dict[str, Any], posts: list[dict[str, Any]],
                       points_cost: int, posts_degraded: bool) -> KolSelectionDetailView:
          safe_detail = normalize_detail_view_payload(detail)
          safe_posts = normalize_detail_view_posts(posts)[:5]
          # select(...).with_for_update() 后创建或覆盖同一身份的缓存行，再 flush。
  ```

  `normalize_detail_view_payload()` 仅允许基础、画像、内容、趋势的设计文档列举键；
  `normalize_detail_view_posts()` 仅允许 `title/nickname/interact/like/comment/collect/
  publish_time/url/platform`，删除未知键和无效值。

- [ ] **Step 4: 运行存储测试确认通过**

  Run: `cd backend && .venv/bin/pytest -q tests/selection/test_detail_views.py`

  Expected: PASS。

- [ ] **Step 5: 提交缓存模型任务**

  ```bash
  git add backend/migrations/versions/0025_kol_selection_detail_views.py backend/app/selection/models.py backend/app/selection/detail_views.py backend/tests/selection/test_detail_views.py
  git commit -m "feat: add versioned KOL detail view cache"
  ```

## Task 2: 详情缓存查询、刷新与 API 契约

**Files:**

- Modify: `backend/app/selection/service.py`
- Modify: `backend/app/selection/router.py`
- Test: `backend/tests/selection/test_kol_selection_detail_endpoints.py`

**Interfaces:**

- Consumes: `DetailViewStore`（Task 1）、`QuickService.kol_detail(user, platform, kw_uid, nickname)`、
  `KolSelectionService.resolve_selection_set()`。
- Produces: `GET /sessions/{session_id}/kol-selection/detail` 与
  `POST /sessions/{session_id}/kol-selection/detail/query`，响应含
  `{set_id, platform, kol_uid, source, detail, posts, points_cost, posts_degraded, fetched_at}`。

- [ ] **Step 1: 写入缓存命中零积分、刷新写缓存与归属拒绝的失败测试**

  生产变更会使这些测试失败：跳过 session 归属校验、缓存命中仍调用 Quick MCP、
  `refresh=true` 不重新查询，或 MCP 失败清空已有缓存。

  ```python
  async def test_cached_detail_returns_zero_points_without_quick_query(auth_client, cached_detail_view, quick_service_spy):
      response = await auth_client.get(
          f"/api/v1/sessions/{cached_detail_view.session_id}/kol-selection/detail",
          params={"set_id": cached_detail_view.selection_set_id, "platform": "douyin", "kol_uid": "uid-1"},
      )
      assert response.status_code == 200
      assert response.json()["source"] == "cache"
      assert response.json()["points_cost"] == 0
      assert quick_service_spy.calls == []

  async def test_refresh_queries_mcp_and_overwrites_only_after_success(auth_client, cached_detail_view, quick_service_stub):
      quick_service_stub.result = ({"followers": 200000}, [{"title": "新热帖", "platform": "douyin"}], False, 20)
      response = await auth_client.post(
          f"/api/v1/sessions/{cached_detail_view.session_id}/kol-selection/detail/query",
          json={"set_id": cached_detail_view.selection_set_id, "platform": "douyin", "kol_uid": "uid-1", "refresh": True},
      )
      assert response.json()["source"] == "refresh"
      assert response.json()["points_cost"] == 20
      assert response.json()["posts"] == [{"title": "新热帖", "platform": "douyin"}]
  ```

- [ ] **Step 2: 运行端点测试确认失败**

  Run: `cd backend && .venv/bin/pytest -q tests/selection/test_kol_selection_detail_endpoints.py`

  Expected: FAIL，路由和详情服务尚不存在。

- [ ] **Step 3: 实现名单身份解析与缓存优先编排**

  在 `KolSelectionService` 添加只读身份方法，先经 `resolve_selection_set()` 校验会话，
  再在该 set 中精确查找 `(platform, kol_uid)`，找不到抛 `selection_item_not_found`。
  从 `KolSelectionDetailSnapshot` 合并安全的 `facts_json/trend_points_json`，但不修改
  `KolSelectionItem.fields_json/score_json`。

  ```python
  async def query_detail_view(self, *, user: User, session_id: str, set_id: str | None,
                              platform: str, kol_uid: str, refresh: bool,
                              quick: QuickService) -> DetailViewResult:
      selection_set, item, snapshot = await self.resolve_detail_subject(...)
      cached = await self._detail_views.get(selection_set_id=selection_set.id, platform=platform, kol_uid=kol_uid)
      if cached is not None and not refresh:
          return DetailViewResult.from_cache(selection_set, item, snapshot, cached)
      detail, posts, degraded, points = await quick.kol_detail(
          user, platform=platform, kw_uid=item.kol_uid, nickname=item.nickname
      )
      cached = await self._detail_views.upsert(...)
      return DetailViewResult.from_query(selection_set, item, snapshot, cached, points, refresh)
  ```

  路由通过可替换依赖 `selection_quick_service()` 创建 `QuickService(db, transport=quick_transport(),
  model=quick_model())`，使 pytest 能替换该边界。将 `InsufficientPointsError` 映射为 409
  `INSUFFICIENT_POINTS`，`QuickCallFailedError`/`ModelAdapterError` 映射为 502
  `QUICK_CALL_FAILED`；`LookupError` 精确映射 `session_not_found`、`selection_set_not_found`、
  `selection_item_not_found` 的 404。

- [ ] **Step 4: 运行端点测试确认通过**

  Run: `cd backend && .venv/bin/pytest -q tests/selection/test_kol_selection_detail_endpoints.py`

  Expected: PASS；覆盖缓存命中、未命中、刷新、跨用户、积分不足、上游失败保留旧缓存。

- [ ] **Step 5: 运行选择模块回归并提交 API 任务**

  ```bash
  cd backend && .venv/bin/pytest -q tests/selection tests/quick/test_kol_detail.py
  cd backend && .venv/bin/ruff check app/selection tests/selection
  git add backend/app/selection/service.py backend/app/selection/router.py backend/tests/selection/test_kol_selection_detail_endpoints.py
  git commit -m "feat: add cached KOL selection detail API"
  ```

## Task 3: 前端 API 契约与可复用详情 BI Dialog

**Files:**

- Modify: `src/api/kolSelection.ts`
- Create: `src/components/KolSelectionDetailDialog.tsx`
- Create: `src/components/KolSelectionDetailDialog.test.tsx`

**Interfaces:**

- Consumes: Task 2 的详情响应、`KolSelectionItem`、现有 `formatExposure/formatNumber`、Recharts。
- Produces: `getKolSelectionDetail()`、`queryKolSelectionDetail()` 与
  `<KolSelectionDetailDialog sessionId setId item onClose />`。

- [ ] **Step 1: 写入 Dialog 的失败渲染测试**

  生产变更会使测试失败：将热帖列表渲染为 6 条、移除主页/原帖安全链接、无缓存时不触发查询、
  或缺趋势数据时崩溃。

  ```tsx
  it('展示缓存详情、主页和至多五条热帖', async () => {
    vi.mocked(getKolSelectionDetail).mockResolvedValue({
      source: 'cache', points_cost: 0, fetched_at: '2026-07-29T12:00:00',
      detail: { followers: 120000, audience_age: { '25-34': 48 }, profile_url: 'https://example.com/kol' },
      posts: Array.from({ length: 6 }, (_, index) => ({ title: `热帖${index}`, platform: 'douyin', url: `https://example.com/${index}` })),
      posts_degraded: false,
    });
    render(<KolSelectionDetailDialog sessionId="s1" setId="set-1" item={item} onClose={vi.fn()} />);

    expect(await screen.findByText('缓存数据')).toBeVisible();
    expect(screen.getByRole('link', { name: '打开主页' })).toHaveAttribute('href', 'https://example.com/kol');
    expect(screen.getAllByRole('article')).toHaveLength(5);
    expect(screen.getAllByRole('link', { name: '查看原帖' })[0]).toHaveAttribute('rel', 'noreferrer');
  });
  ```

- [ ] **Step 2: 运行前端测试确认失败**

  Run: `npm run test -- --run src/components/KolSelectionDetailDialog.test.tsx`

  Expected: FAIL，因为 API 客户端与 Dialog 组件尚不存在。

- [ ] **Step 3: 实现 API 类型和居中 Dialog**

  在 `src/api/kolSelection.ts` 定义：

  ```ts
  export interface KolSelectionDetailResponse {
    set_id: string;
    platform: string;
    kol_uid: string;
    source: 'cache' | 'query' | 'refresh' | 'missing';
    detail: Record<string, unknown>;
    posts: Array<Record<string, unknown>>;
    points_cost: number;
    posts_degraded: boolean;
    fetched_at: string | null;
  }

  export function queryKolSelectionDetail(sessionId: string, input: {
    setId?: string; platform: string; kolUid: string; refresh: boolean;
  }): Promise<KolSelectionDetailResponse>;
  ```

  Dialog 使用 `role="dialog" aria-modal="true" aria-labelledby="kol-detail-title"`；
  初次加载先 GET，再在 `source="missing"` 时自动 POST `refresh=false`；刷新按钮 POST
  `refresh=true`。在 effect 内保存触发元素，关闭时恢复焦点；注册 Esc 并清理监听。

  图表组件必须分别接收安全的年龄、地区、兴趣和趋势数据；输入为空时渲染带说明的空态，而不把
  `undefined` 传入 Recharts。头部和热帖链接统一：

  ```tsx
  <a href={safeUrl} target="_blank" rel="noreferrer" aria-label="打开主页">主页</a>
  ```

  卡片正文由“概览指标 / 评分维度 / 受众画像 / 内容趋势 / 最新热帖”五个小组件组成，保持
  `KolSelectionDetailDialog.tsx` 只负责编排和请求状态。

- [ ] **Step 4: 运行 Dialog 测试确认通过**

  Run: `npm run test -- --run src/components/KolSelectionDetailDialog.test.tsx`

  Expected: PASS；覆盖缓存、查询 loading、5 条截断、主页/热帖链接、缺失图表、Esc 关闭和刷新积分提示。

- [ ] **Step 5: 提交前端详情 Dialog 任务**

  ```bash
  git add src/api/kolSelection.ts src/components/KolSelectionDetailDialog.tsx src/components/KolSelectionDetailDialog.test.tsx
  git commit -m "feat: add KOL selection detail BI dialog"
  ```

## Task 4: 从圈选名单卡片进入详情并保证交互隔离

**Files:**

- Modify: `src/components/UniversalReport.tsx`
- Modify: `src/components/UniversalReport.test.tsx`

**Interfaces:**

- Consumes: `KolSelectionDetailDialog`（Task 3）、当前 `sessionId`、`selectedSetId` 与列表项。
- Produces: 可点击、可键盘访问的名单卡片；卡片收藏和原有外链不触发详情弹窗。

- [ ] **Step 1: 写入名单入口的失败测试**

  生产变更会使测试失败：卡片点击没有打开 Dialog、没有把所选名单版本传入、收藏按钮冒泡打开
  Dialog，或 Esc 关闭后焦点没有返回触发卡片。

  ```tsx
  it('点击当前名单的达人卡片，打开同一 set 的详情弹窗且收藏不冒泡', async () => {
    render(<UniversalReport sessionId="session-1" selectionCount={1} favorites={[]} />);
    fireEvent.click(await screen.findByRole('tab', { name: '圈选达人 (1)' }));
    const card = await screen.findByRole('button', { name: /查看 美食小探 的详情/ });
    fireEvent.click(card);
    expect(await screen.findByRole('dialog', { name: /美食小探/ })).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: '收藏 美食小探' }));
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
  });
  ```

- [ ] **Step 2: 运行入口测试确认失败**

  Run: `npm run test -- --run src/components/UniversalReport.test.tsx`

  Expected: FAIL，因为卡片没有详情触发语义且未挂载 Dialog。

- [ ] **Step 3: 接入卡片状态与 Dialog**

  在 `KolPanel` 增加 `selectedDetailItem` 状态，切换 `sessionId` 或 `selectedSetId` 时清空。
  `KolSelectionCard` 外层采用 `<button type="button">`，使用 `aria-label="查看 {nickname} 的详情"`；
  收藏按钮使用 `event.stopPropagation()`，不得嵌套 button。为避免非法嵌套，卡片容器采用
  `section`，详情触发放置为绝对覆盖的透明 button，收藏保持同级更高层级按钮。

  ```tsx
  {selectedDetailItem && sessionId && (
    <KolSelectionDetailDialog
      sessionId={sessionId}
      setId={selectedSetId}
      item={selectedDetailItem}
      onClose={() => setSelectedDetailItem(undefined)}
    />
  )}
  ```

- [ ] **Step 4: 运行入口与 Dialog 联合测试确认通过**

  Run: `npm run test -- --run src/components/UniversalReport.test.tsx src/components/KolSelectionDetailDialog.test.tsx`

  Expected: PASS。

- [ ] **Step 5: 运行前端验证并提交入口任务**

  ```bash
  npm run lint
  npm run build
  git add src/components/UniversalReport.tsx src/components/UniversalReport.test.tsx
  git commit -m "feat: open KOL detail from selection cards"
  ```

## Task 5: 集成验证、运行记录与交付检查

**Files:**

- Modify: `changelog/2026-07-29.md`
- Modify: `docs/runbooks/phase-2-runtime.md`

**Interfaces:**

- Consumes: Tasks 1–4 的迁移、API 与 Dialog。
- Produces: 可部署迁移说明、缓存/刷新/积分验收记录。

- [ ] **Step 1: 写入运行手册的失败场景检查清单**

  将以下可复现验收动作写入 runbook：首次详情查询实际扣分、再次打开零积分、刷新重新扣分、
  积分不足不覆盖旧缓存、热帖降级仍保留详情、跨用户访问返回 404。

- [ ] **Step 2: 执行数据库迁移与端到端人工验收**

  ```bash
  cd backend && .venv/bin/alembic upgrade head
  ```

  在本地登录后：从圈选达人点击一名未缓存达人，记录钱包变化与弹窗的 5 条热帖；关闭后再次
  打开确认 `缓存数据` 和零积分；点刷新确认更新时间和积分变化；对余额不足账号确认旧缓存仍可读。

- [ ] **Step 3: 运行完整相关验证**

  ```bash
  cd backend && .venv/bin/pytest -q tests/selection tests/quick/test_kol_detail.py
  cd backend && .venv/bin/ruff check app tests
  npm run test
  npm run lint
  npm run build
  ```

  Expected: 所有命令通过；生产构建仅允许既有 bundle 大小警告。

- [ ] **Step 4: 更新变更日志与运行手册**

  记录详情缓存的版本隔离、首次/刷新才扣费、每次最多 5 条热帖，以及迁移 `0025` 已执行的
  环境范围。不得记录密钥、完整 MCP 原始响应或用户敏感数据。

- [ ] **Step 5: 提交验收与文档任务**

  ```bash
  git add changelog/2026-07-29.md docs/runbooks/phase-2-runtime.md
  git commit -m "docs: record KOL detail BI cache operations"
  ```

## 计划自检

- 设计覆盖：缓存身份、首次/刷新计费、MCP 复用、5 条热帖、公开链接、居中 Dialog、BI 图表、
  缺失/降级/积分不足、权限隔离、测试与运行手册均对应至少一个任务。
- 类型一致性：后端统一输出 `DetailViewResult`/详情响应；前端统一消费
  `KolSelectionDetailResponse`；名单身份始终使用 `setId + platform + kolUid`。
- 范围控制：未引入跨版本缓存、自动定时刷新、额外图表依赖或新的独立页面。
