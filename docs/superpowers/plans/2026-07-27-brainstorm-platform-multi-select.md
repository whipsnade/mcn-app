# brainstorm 平台多选实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 平台类澄清问题支持多选（chips 切换选中 + 确认拼接填入），merge_profile 平台并集不丢值；单选问题交互不变。

**Architecture:** 后端 `BrainstormQuestion.multi`（模型按问题类型判定，prompt 引导）+ metadata 透传 + `merge_profile` platforms 并集合并；前端 `BrainstormMetadata.multi` + ChatArea 多选 chips 态。

**Tech Stack:** FastAPI + Pydantic（后端），React + TypeScript + Vitest（前端）。

**Spec:** `docs/superpowers/specs/2026-07-27-brainstorm-platform-multi-select-design.md`

**验证命令（每个 Task 的 Commit 前必跑）：**

```bash
cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/brainstorm -q
# 前端（仓库根目录）
npm run test && npm run lint
```

---

### Task 1: 后端 multi 字段 + merge_profile 并集

**Files:**
- Modify: `backend/app/brainstorm/schemas.py`（`BrainstormQuestion.multi`、`merge_profile`）
- Modify: `backend/app/brainstorm/service.py:128-145`（metadata 加 multi）
- Modify: `backend/app/model/prompts.py:65`（BRAINSTORM_SYSTEM_TEXT 补 multi 规则）
- Test: `backend/tests/brainstorm/test_brainstorm.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
def test_question_multi_defaults_false() -> None:
    q = BrainstormQuestion(text="哪个品牌？", options=["海底捞"])
    assert q.multi is False


def test_merge_profile_platforms_union_preserves_order_and_dedupes() -> None:
    base = BrainstormProfile(brand="问界", platforms=["douyin", "xiaohongshu"])
    incoming = BrainstormProfile(platforms=["xiaohongshu", "bilibili"])
    merged = merge_profile(base, incoming)
    assert merged.platforms == ["douyin", "xiaohongshu", "bilibili"]


def test_merge_profile_platforms_incoming_empty_keeps_base() -> None:
    base = BrainstormProfile(brand="问界", platforms=["douyin"])
    merged = merge_profile(base, BrainstormProfile())
    assert merged.platforms == ["douyin"]


# HTTP 级：brainstorm 响应 assistant metadata.brainstorm.multi 与模型输出一致（FakeBrainstormModel 输出 question.multi=true）。
```

先读 `merge_profile` 现状（schemas.py:72-81）与 `BrainstormProfile.platforms` 类型（list 还是 list[Platform] 字符串枚举——以源码为准调整断言取值）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/brainstorm -q -k "multi or merge"`
Expected: FAIL（multi 字段不存在 / 并集未实现）

- [ ] **Step 3: 实现**

`backend/app/brainstorm/schemas.py`：
- `BrainstormQuestion` 加 `multi: bool = False`。
- `merge_profile`：platforms 改并集（保序去重，先旧后新；incoming 为空列表时保留 base——注意区分「未提供」与「明确空」：按现状字段语义，空数组视为未提供，与 spec 遗留事项一致），其余字段逻辑不动。

`backend/app/brainstorm/service.py`：`brainstorm_metadata` 加 `"multi": bool(output.question.multi) if output.question is not None else False`（按现有 options 构造风格就近）。

`backend/app/model/prompts.py` 的 `BRAINSTORM_SYSTEM_TEXT` 在 ready=false 规则行后补：

```
question.multi 标识该问题是否允许多选：platforms（渠道）问题必须 multi=true 且提问文案引导「可多选」，其余问题 multi=false。
```

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest tests/brainstorm tests/goals -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/brainstorm/schemas.py backend/app/brainstorm/service.py backend/app/model/prompts.py backend/tests/brainstorm/test_brainstorm.py
git commit -m "feat: brainstorm 问题 multi 标记与平台画像并集合并"
```

---

### Task 2: 前端多选 chips + 确认填入

**Files:**
- Modify: `src/api/contracts.ts`（`BrainstormMetadata` 加 `multi?: boolean`，约 :91-95）
- Modify: `src/components/ChatArea.tsx`（chips 区域，约 :312-331）
- Test: `src/components/ChatArea.test.tsx`（追加）

- [ ] **Step 1: 写失败测试**

```tsx
// 1. 最新消息 brainstorm.options + multi=true：chips 可点击切换选中态（样式/aria-pressed），
//    确认按钮把选中项以「、」拼接填入输入框并清空选中；未选时确认禁用。
// 2. multi=false（或无 multi）：点击 chip 仍整体替换输入框（现状回归）。
// 3. 切换到新的 assistant 消息（id 变化）后选中态重置。
```

先读该文件既有 chips 用例构造（消息 fixture 的 brainstorm metadata 形状）。

- [ ] **Step 2: 跑测试确认失败**

Run: `npm run test -- src/components/ChatArea.test.tsx`
Expected: FAIL（多选交互不存在）

- [ ] **Step 3: 实现**

`src/api/contracts.ts`：`BrainstormMetadata` 加 `multi?: boolean;`（`src/types.ts` 若只是引用 contracts 类型则无需改，先确认）。

`src/components/ChatArea.tsx`：
- 新 state：`const [selectedOptions, setSelectedOptions] = useState<string[]>([]);` + 随 `latestAssistantMessageId` 变化重置（useEffect）。
- chips 渲染分支（`brainstormOptions` 非空处）：
  - `msg.brainstorm?.multi === true`：每个 chip 用 `aria-pressed={selectedOptions.includes(option)}`，点击切换选中集合；选中态样式高亮（如 `bg-indigo-100 border-indigo-400`）；chips 旁加「确认」按钮（`disabled={selectedOptions.length === 0}`），点击 `fillInput(selectedOptions.join('、'))` 并 `setSelectedOptions([])`。
  - 否则：现状 `fillInput(option)` 单选行为不变。
- clarify.options 路径无 multi，自然走单选分支。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `npm run test && npm run lint`
Expected: PASS、tsc 退出 0

- [ ] **Step 5: Commit**

```bash
git add src/api/contracts.ts src/components/ChatArea.tsx src/components/ChatArea.test.tsx
git commit -m "feat: 平台问题多选 chips 与确认拼接填入"
```

---

### Task 3: 全量验证 + changelog

- [ ] **Step 1: 全量验证**

```bash
cd backend && .venv/bin/ruff check app tests && DATATAP_MCP_TOKEN=test-only-token .venv/bin/pytest -q
cd .. && npm run test && npm run lint && npm run build
```

- [ ] **Step 2: changelog 并提交**

`changelog/2026-07-27.md` 追加「brainstorm 平台多选」：限制点（chips 替换输入框 + merge_profile 覆盖）、改动（multi 标记/并集/多选 chips）、验证、遗留（multi 准确率观察、并集只增不减、options ≤4）。

```bash
git add -f changelog/2026-07-27.md
git commit -m "docs: 记录 brainstorm 平台多选"
```

---

## 备注

- 实现中如字段名/类型与计划不一致（如 platforms 元素是 Platform 枚举还是 str），以源码为准。
- 不改 planner clarify 的 GoalQuestion（spec YAGNI）。
