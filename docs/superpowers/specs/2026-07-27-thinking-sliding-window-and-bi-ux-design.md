# 思考滑动窗口 + BI 加载动态 + KOL 圈选 Top20 + KOL/品牌报告串台修复 设计

日期：2026-07-27
状态：已确认（滑动窗口方案用户已选；后三项为用户直接报告的问题）

## 背景

四个体验问题：

1. **思考截断后空块**：思考内容两级限额（单块 12k、turn 30k）是「保留头部、截掉尾部」，
   长 agent 任务（20+ 轮）耗尽 turn 额度后，后续所有思考块为 len=0 空块，最新推理
   （最有用）完全不可见，空块连提示文案都没有。数据库实证：问界会话第 12 块起全部
   len=0 truncated=True。
2. **品牌分析 BI 显示慢且无加载反馈**：`TypedReportPanel` 两段串行拉取（版本列表 →
   详情），详情拉取期间**没有 loading 态**——会闪现 emptyText（「暂无…」）或旧内容；
   版本列表的 loading 也只是静态文字「加载中…」。
3. **圈选达人全量渲染**：Manner 会话名单 142 条全部展示，用户要求按互动率倒序取 Top 20。
4. **KOL 分析与品牌分析串台**：「达人」Tab 的 KOL 分析子 Tab 显示了品牌分析报告。
   根因：`report.updated` SSE 事件 payload（`report_id/version/phase/label`）**不含
   report_type**（auto_kol_analysis 与 brand/campaign goal 报告两个发射点同构），
   前端 reducer（`taskEvents.ts:293-303`）对任何 report.updated 都设置
   `visibleAnalysisReportId`，useWorkspace 据此拉取详情渲染进 KOL 分析子 Tab。
   后端会话 DTO 的 `latest_analysis_report` 已按 kol_analysis 过滤，仅 SSE 路径泄漏。

### 已确认决策

- 思考超限策略：**滑动窗口保留最新**（单块保尾部、turn 折叠最旧块），不单纯提高上限。
- KOL 圈选：面板按 `engagement_rate` 倒序展示 Top 20（null 排最后）。

## 设计

### 1. 思考滑动窗口（后端 `thinking/service.py` + `sanitizer.py`）

- **单块保尾部**：`sanitize_thinking` 截断从「保头 + 后缀」改为「保尾 + 前缀标记」
  （前缀 `…（早期内容已折叠）`，长度计入 max_chars）。实时 delta 因 public_text 不再是
  前缀递增，走既有 snapshot 替换路径（reducer 本就支持）。
- **turn 折叠最旧块**：新增内部操作——当 turn 预算不足时，按完成顺序折叠最旧块：
  其 `content` 替换为占位符 `「早期思考已折叠」`（约 9 字符），释放额度给新块；
  折叠是幂等的（已折叠块不重复处理）。实时 `_delta` 与终态 `_fit_turn_budget` 共用
  同一预算入口，保证额度语义一致。
- **空块修复**：折叠机制下预算不再耗尽为 0；防御性地，若 content 为空且 truncated，
  事件与持久化内容保底为占位文案而非空串。
- 已持久化的历史块不受影响（只改运行中与终态写入路径）。

### 2. BI 报告面板加载动态（前端 `UniversalReport.tsx` 的 `TypedReportPanel`）

- 新增 `detailLoading`：详情拉取（`getAnalysisReport`）期间为 true。
- `loading || detailLoading` 时渲染动画加载态：`Loader2` 旋转图标 +
  `useLoadingMessage` 分阶段文案（复用既有 hook，DEFAULT_LOADING_STAGES），替代
  静态「加载中…」与详情期间的 emptyText 闪现。版本列表/详情都拉完后才渲染
  `ReportBlocks` 或 emptyText。

### 3. KOL 圈选 Top20（前端 `UniversalReport.tsx` 的 `KolPanel`）

- `selectionItems` 渲染前：按 `selectionMetric(item, 'engagement_rate')` 倒序
  （null/非数值排最后，原顺序稳定）取前 20。
- 名单 > 20 时在列表顶部加一行摘要：`共 N 位达人，按互动率展示 Top 20`。
- 子 Tab 计数标签（`圈选达人 (N)`）与导出逻辑（Excel 有自己的 top50/排序）不变。

### 4. KOL/品牌报告串台修复（后端 payload + 前端两道路径）

- **后端**：两个 `report.updated` 发射点的 payload 增加 `report_type`
  （auto_kol_analysis 为 `"kol_analysis"`；`_ANALYSIS_GOAL_TABLE` 路径为 goal 对应的
  report_type）。纯增量字段，向后兼容。
- **前端 reducer**（`taskEvents.ts`）：仅当 `payload.report_type === 'kol_analysis'`
  时才设置 `visibleAnalysisReportId`；**缺省（历史事件重放无该字段）按现状设置**
  （兼容旧 kol 事件；旧 brand/campaign 事件重放会泄漏，属可接受的过渡近似，
  新事件全部带类型）。
- **前端 useWorkspace 防线**：拉取报告详情后校验 `report_type === 'kol_analysis'`，
  否则不挂到 `analysisReport`（即使 reducer 漏判也不显示串台内容）。

### 不做的事（YAGNI）

- 不提高思考限额数值（滑动窗口替代）；不改思考 SSE 事件类型与 metadata 结构
  （version 1 不变，占位符是普通文本）。
- 圈选接口/导出/收藏行为不变（纯面板展示裁剪）。
- TypedReportPanel 的失败态 artifact 提示、版本下拉不变。
- 历史 report.updated 事件不回填 report_type（重放近似已声明）；品牌/活动 Tab 的
  TypedReportPanel 不受影响（它按 report_type 拉版本列表，本就正确）。

## 测试策略

- 思考：单块超限保尾部 + 前缀标记；turn 超额折叠最旧块（最新块完整、最旧块为
  占位符、总长度有界）；折叠幂等；既有 sanitize/service 用例按新语义更新
  （保头 → 保尾的断言方向变化）。
- TypedReportPanel：详情拉取期间渲染动画加载态而非 emptyText；拉完后渲染报告。
- KolPanel：>20 条时只渲染 20 条且按 engagement_rate 倒序、null 在最后、摘要行
  文案；≤20 条时无摘要行、全部渲染。
- 串台：后端两处 report.updated payload 带 report_type；reducer 只在
  report_type=kol_analysis（或缺省）时设置 visibleAnalysisReportId，brand_analysis
  不设置；useWorkspace 拉取详情后 report_type 不符不挂 analysisReport。

## 遗留事项

- 滑动窗口改变「完整回放」语义：刷新恢复时早期思考显示为折叠占位，属设计取舍。
- 空块防御文案为兜底路径，正常流程不再触发。
