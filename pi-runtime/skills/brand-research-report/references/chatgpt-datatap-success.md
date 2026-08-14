# 参数化 DataTap 成功案例要点

本引用仅提供可复用的研究判断，不包含来源品牌、日期、数值、凭证或固定工具顺序。

- 确认用户的实体、平台、时间窗口和比较口径；实体存在歧义时请求澄清。
- 阅读实时工具 Schema，按当前问题分阶段收集概览、趋势、维度和帖子 Evidence，并核对字段
  口径与空值。
- 某项能力失败或结果为空时，如实记录；只在已有等价 Evidence 时使用确定性聚合，并披露
  替代来源与限制。
- 用 Builder feedback 的 availability、coverage 与 limitations 判断补查优先级，不补造数据。
- 只有 Evidence lineage、强类型校验和发布条件都满足时才完成；受限数据可以生成 partial
  Artifact，但结论必须限定在证据覆盖范围内。
