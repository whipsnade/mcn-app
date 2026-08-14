# 新 Codex 会话初始化提示词：执行 Pi RPC POC

请在下面整段提示词之后开始工作，不要省略预热、隔离或 Gate：

```text
你负责 KOL Insight AI 的 Pi Agent Runtime 方案 A（Pi RPC POC）开发。

仓库：/Users/hanxiang/Works/Projects/codex/mcn-app
设计基线分支：codex/agent-runtime

目标不是重新设计，而是严格执行已经确认的方案 A。先确认包含以下文档的最新提交已经在
设计基线中；如果文档还只是另一个工作树里的未提交文件，立即停止并告知用户，不能从旧
基线自行猜测实施：

1. docs/superpowers/specs/2026-08-07-pi-agent-runtime-integration-design.md
2. docs/superpowers/specs/2026-08-07-marketing-mcp-gateway-future-design.md
3. docs/superpowers/plans/2026-08-07-pi-rpc-poc.md
4. changelog/2026-08-07.md 中“Pi Agent Runtime 集成设计与方案 A 实施计划”章节

强制工作方式：

- 始终使用中文沟通。
- 先完整读取仓库 AGENTS.md、最新 3 篇 changelog、主设计和实施计划。
- 使用 superpowers:using-git-worktrees 创建独立工作树和 codex/pi-runtime-poc 分支；不得在
  当前 dirty 的 codex/agent-runtime 工作树直接开发。
- 使用 superpowers:executing-plans 严格逐 Task 执行
  docs/superpowers/plans/2026-08-07-pi-rpc-poc.md；每个 Task 遵循 TDD、先红后绿、聚焦测试、
  自审、独立 commit，再进入下一个 Task。
- 结构性代码调查优先使用 CodeGraph：context 后最多一次 explore；文字、配置和日志才用 rg。
- 不要改写已确认设计。如果实测 Pi API 与计划不同，只在 Task 1 记录官方当前 API、给出最小
  兼容修订并等待确认；不允许偷偷换框架、换模型或退回自研编排。

不可违反的架构边界：

- 只做方案 A。不得开发方案 B 的生产 Gateway、租户、License、积分、管理端、并发，也不得
  实施方案 C Marketing MCP Gateway。
- POC 数据库必须精确为 kol_insight_pi_poc；任何其他数据库名立即 fail closed。不得读写
  kol_insight 或 kol_insight_test。
- 积分不属于 POC 评价指标，也不能接触真实用户钱包。Pi 路径不创建钱包、不预留、不扣减、
  不结算；Current 基线路径必须保持原生 WalletService reserve/settle/release，并仅使用
  kol_insight_pi_poc 中每案例独立的一次性测试钱包。不得修改或绕过 Current 计费代码。
- Current Runtime 与 Pi 必须使用完全相同的 provider、model、thinking level、用户输入、
  会话历史、日期窗口和 DataTap 凭证；同模型无法在 Pi 使用时 Gate 直接失败，不得替换模型。
- Pi 每个 Run 使用独立临时 Session/进程，--mode rpc --no-session --no-builtin-tools
  --no-context-files，关闭自动发现，只显式加载项目 Extension 和 Skills。
- Pi 直接连接 DataTap MCP。透明 Hook 只能旁路记录 ToolCall、原始结果、错误和 Evidence；不得
  修改工具名、参数、结果或错误，不得自动重试、拆分、改换工具、做意图路由或空结果熔断。
- DataTap/模型/内部 Run token 只能进入临时进程内存或环境变量，禁止出现在 Prompt、Skill、
  stdout、事件、Artifact、fixture、测试快照、QA 文档和 git diff。
- Pi 默认 Shell、read/write/edit/任意 HTTP 工具全部禁用。只开放 DataTap MCP、受控历史读取、
  六类 Builder 和 publish_artifacts。
- 强类型报告必须走现有 Builder + ArtifactPublicationService；Excel、BI、营销结论必须绑定同一
  不可变 Artifact Version；不允许 Pi 手写正式 payload。
- DataTap 错误、空结果和超时原样交给 Pi。代码不做业务重试。报告可以 partial，但数值必须有
  Evidence lineage，不能编造。
- 不增加模型 Reviewer；Thinking 前端语义保持默认折叠。
- 不使用 LibreOffice，不做截图或视觉审核；Excel 只做 openpyxl 结构、数据、图表对象与版本
  一致性验证。
- 不覆盖 outputs 旧轮次，不通过重复真实 UAT 掩盖供应商抖动。

执行顺序：

1. 先检查 git status、分支、迁移 head 和 CodeGraph 状态，确认独立工作树干净。
2. 从实施计划 Task 1 开始。Task 1 必须通过官方当前 Pi 文档/npm 实测锁定精确包版本、RPC
   JSONL、Extension、Skill、tool flags，并验证 Pi 能使用当前 Runtime 的同一模型。
3. 按 Task 2–8 完成隔离门禁、RPC client、DataTap Extension、Evidence 旁路、内部工具、
   Skills、Run/Event 适配和对比 Harness。
4. Tasks 1–8 的单元/集成测试全部通过后，先执行修订后的 Task 8A：给 Current fixture 补齐
   隔离钱包、默认渠道权限、真实 model 和 profile_version=v1，同时保持 Pi 无钱包；通过后
   才执行 Task 9 一轮真实六场景 current/pi 对比。
5. 运行计划列出的后端、pi-runtime、前端全量回归；真实服务结果不因失败而反复重跑。
6. 只有真实六场景至少启动并创建 round 后才能写 Gate A PASS 或 FAIL；若在真实调用前因
   配置或基础设施阻断，只能写 Gate A BLOCKED / NOT RUN。一个效果硬门槛失败才是 FAIL。
7. Gate A 结束后停止。不要自行进入方案 B。提醒用户：通过后应基于实测 Pi API 新建方案 B
   详细计划；Pi 链路稳定一个发布周期后再提醒评估方案 C。

每个 Task 的汇报格式：

- Task 与交付结论
- 修改文件
- 红灯证据
- 绿灯命令与准确结果
- 安全/数据隔离核对
- 发现的计划偏差
- commit hash
- 是否允许进入下一 Task

开始时先用两三句话复述你理解的边界，然后执行预热和 Task 1；不要重新向用户重复已经在
设计中确认的问题。
```
