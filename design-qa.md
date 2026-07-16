# Task5 数据分析面板视觉验收

日期：2026-07-16

参考图：`/var/folders/89/n931hfxd2gl_1qnxtx3xbxy40000gn/T/codex-clipboard-2e38838b-f9b0-4059-b703-835fa834b0d9.png`

实现截图：`output/playwright/bi-analytics-visual.png`、`output/playwright/bi-analytics-audience.png`

## 验收项

- 右侧面板保持约 420px 宽，白色卡片、浅灰边框、紫色主色、圆角阴影和纵向滚动与原型一致。
- “报告概览 / 数据分析”Tab 可点击、键盘可达，选中态使用紫色下边框；切换会话时通过 `sessionId` 重置到概览并清除旧面板状态。
- 数据分析包含三张指标卡、情感极性环图与比例条、热词、曝光趋势折线图、年龄柱状图、性别比例条和地区 Top5。
- 通过终态任务门禁和 `available/value` 判断，不可证明的数据保留卡片框并显示“数据不足”，不以 0 代替未知值。
- 浏览器控制台 error/warn：无。
- 前端组件测试、类型检查：通过。

## 结果

final result: passed

备注：Playwright 独立启动测试因本机 Chromium 沙箱权限和测试后端启动时 DataTap DNS 不可用而无法作为验收依据；改用当前本地前端服务和浏览器现有登录态完成交互与视觉截图验证。
