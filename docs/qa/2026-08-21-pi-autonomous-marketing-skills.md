# PI 自主营销 Skills：离线 QA 与迁移记录

日期：2026-08-21
分支：`codex/pi-autonomous-marketing-skills-implementation`

## 范围与结论

本记录覆盖数据库 Skill Revision/Activation、Run Skill Snapshot、Pi Gateway 原生快照、
`analysis_report_v1`/`workbook_v1`、主报告完成门禁、管理端 Skill 工作台、通用报告 BI 视图和
Native Skill 文案迁移的离线实现检查。

结论是“离线实现链路已覆盖，真实外部验证未执行”。这不是真实模型、DataTap、钱包、生产库、
部署或 Web UAT 的通过声明。

## 设计覆盖矩阵

| 区域 | 离线断言 | 结果 |
| --- | --- | --- |
| Skill 注册表 | Revision 不可变；Activation 只切换指针；digest、租户灰度和回滚幂等 | Task 1/2 定向 pytest 已通过 |
| Run Snapshot | 新 Run 解析数据库激活；existing/child/resume/recovery 复用冻结快照；digest 漂移 fail-closed | Task 3 定向回归已通过 |
| Native Gateway | Skill 目录 0700、正文 0600；关闭 cwd/用户/项目自动发现；显式加载当前 Run 快照 | Task 4 定向 Vitest/typecheck 已通过 |
| 通用 Report | `analysis_report_v1` 判别联合；7 类 block；null/partial/restricted；长尾不截断；URL 白名单 | Task 9 前端定向回归 43 个文件、316 个测试通过 |
| Workbook | Report Version → `workbook_v1` 同版导出；标准类型路径不回归 | Task 6 后端定向回归已通过 |
| 完成门禁 | 普通用户 Run 要求当前 Run 顶层主 Artifact；clarification/utility 例外；child/history 不满足 | Task 7 定向回归 20 + 13 通过 |
| 管理工作台 | Revision 列表/编辑/校验/Diff/租户灰度/全量激活/确认回滚/审计字段/idempotency | Task 8 前端回归 313 测试通过 |
| Native 文案 | 模型自主决策、Run Snapshot、Tool Contract、Evidence、partial、结构化校验反馈；无固定顺序/旧桥接必经 | `tests/skills.test.ts` 6/6 通过 |

## 已执行命令

以下命令均为离线测试；没有注入真实模型或 DataTap 凭证：

```text
npm test -- --run src/api/agentArtifacts.test.ts src/components/artifacts/AnalysisReportView.test.tsx src/components/artifacts/ArtifactWorkspace.test.tsx
→ 43 个文件、316 个测试通过

npm --prefix pi-runtime test -- --run tests/skills.test.ts
→ 1 个文件、6 个测试通过
```

`npm run lint` 已执行。Task 8/9 新增前端类型错误已清零，但仓库现有以下问题仍阻断全仓 TypeScript：

- `pi-gateway/src/main.ts` 的 `artifactContract: unknown` 类型错误 2 处；
- `pi-runtime` 未安装的 `pi-mcp-adapter`、`@earendil-works/pi-coding-agent`、`typebox` 模块。

`pi-runtime/tests/internal-tools.test.ts` 同样因当前 worktree 没有 `pi-runtime/node_modules/typebox`
而无法收集；本阶段没有擅自联网安装依赖。`internal-tools.ts` 的兼容标识已通过静态源码检查，
并保留 `load_marketing_skill` 定义。

## 关键安全断言

- Native Skill 正常路径只认 Run Snapshot 注入的目录、Tool Contract 和 Root Policy；不依赖 cwd、
  用户目录、项目目录或本机工作树。
- `pi-runtime` 旧内部工具面明确为 `compatibility`；`load_marketing_skill` 仅作为迁移期 POC
  入口保留，Native Skill 文案不把旧 Builder/发布工具写成生产必经步骤。
- MCP 标准结果直接回到模型；不恢复 Evidence Bridge，不引入 `mcp_result_v1`，unknown 不自动重放。
- 通用报告的业务 null 不转成 0；restricted/partial/unavailable 与 limitation 可见；表格保留全部
  返回行；前端链接仅允许 `http`/`https`。
- 管理端回滚使用可访问 `ConfirmDialog`，不调用 `window.confirm`/`window.prompt`；写请求携带
  `Idempotency-Key`。
- 文案、Skill 案例和测试未包含密钥、Bearer、DSN、固定来源实体或真实业务凭证。

## 明确未执行项

本次不执行：真实模型调用、真实 DataTap/MCP 调用、钱包/积分扣费、生产或开发业务库写入、部署、
Web UAT、Corpus Replay、Stage 2A/2B、60-observation、长周期稳定性循环及外部服务 smoke。
任何一项都必须在单独授权消息、隔离租户/数据库、append-only 证据目录和 reviewer 封口条件下进行。

## 遗留与下一步

- 补齐 `pi-runtime` 依赖后再运行 `tests/internal-tools.test.ts` 和 `npm run typecheck`；这不是本阶段
  通过真实运行时的证明。
- 继续按计划审查 Native 文案、Gateway runbook 与 Task 11 的最终分阶段验证；未获得授权前不启动
  真实服务或生产切流。
