# DataTap MCP 服务与工具能力说明

本文按「服务 → 提供什么 → 能实现什么效果」梳理当前系统注册的全部 MCP 工具（29 个，已审核启用）。
入参 Schema 快照见 `docs/datatap-mcp-tools.md`；工具启用/隔离流程见 `AGENTS.md` 配置与安全一节。

## 服务总览

| 服务 | 提供什么 | 能实现什么效果 |
|---|---|---|
| 聆媒洞察 insight-cube | 内容/声量数据：原帖检索、统计分析、标签匹配、榜单、分析对象检索 | 品牌舆情分析、声量/曝光/情感统计、受众画像、爆贴榜单、热帖检索 |
| 达人精选 social-grow | 达人维度：五平台 KOL 搜索、达人详情、提及标签匹配、分类标签字典 | 投放选人、预算内达人推荐、达人绩效与粉丝画像、带货/商业数据分析 |
| 内容选题 social-grow-content | 内容热词榜：热词字典/榜单/下钻帖子、内容选题 | 品类口径的爆贴榜（7日/月榜）、消费热词趋势、选题灵感 |
| bilibili | B站内容/用户搜索、弹幕 | B站专属内容与 UP 主分析 |

## 聆媒洞察 insight-cube（12 个）

| 工具 | 效果 |
|---|---|
| match_best_tag | 把"美食/海底捞"匹配成标准品类/品牌标签（统计查询的前置） |
| query_analysis | 品牌声量、互动量、情感极性（正/中/负）统计 |
| statistic_overview | 品牌/关键词社交搜索整体概览（大盘指标） |
| statistic_trend | 按天/按时段的声量与曝光走势 |
| statistic_user_profile | 受众画像：年龄、性别、省份分布 |
| statistic_hot_user | 品牌相关热门用户/传播达人榜 |
| statistic_hot_topic | 热门话题与声量聚类（评论热词来源） |
| statistic_category_rank | 品类及子品类市场表现与声量排行 |
| statistic_brand_activity | 品牌相关活动列表与互动数据 |
| query_raw_posts | 原帖明细检索（按标签/关键词/作者，按互动数排序）——爆贴主力工具 |
| query_rank_list | 热搜话题榜单（微博/抖音/知乎/百度） |
| analysis_target_search | 分析对象规则检索（预定义圈选对象） |

## 达人精选 social-grow（8 个）

| 工具 | 效果 |
|---|---|
| kol_xiaohongshu_search | 小红书达人筛选（粉丝/地域/受众/报价/提及标签） |
| kol_douyin_search | 抖音达人筛选（同上） |
| kol_bilibili_search | B站达人筛选（含报价筛选参数） |
| kol_weibo_search | 微博达人筛选（含双档报价筛选） |
| kol_wechat_search | 微信达人筛选（含三档报价筛选） |
| kol_detail | 达人详情：受众画像/发帖统计/价格趋势/带货数据（抖音购物车星图、小红书蒲公英） |
| kol_match_mentions_tag | 品牌/品类提及标签匹配（KOL 搜索的标签前置） |
| kol_class_tag_dictionary | KOL 分类标签字典 |

## 内容选题 social-grow-content（5 个，仅小红书）

| 工具 | 效果 |
|---|---|
| hotwords_dictionary | 热词参数字典：行业/品类/可查日期范围（热词查询前置） |
| hotwords_list | 消费热词榜单（7日榜/月榜，预计算、快而稳） |
| hotwords_posts | 热词下钻关联帖子（爆贴的品类口径路径） |
| topic_list | 内容选题榜（按美食/母婴等主题） |
| topic_posts | 选题关联帖子列表 |

## bilibili（4 个）

| 工具 | 效果 |
|---|---|
| general_search | B站内容关键词搜索 |
| search_user | B站用户搜索 |
| get_video_danmaku | 视频弹幕数据 |
| get_precise_results | B站精确搜索结果 |

## 选择建议（给模型与运营参考）

- **爆贴/热帖**：首选内容选题的热词链路（预计算榜单，快而稳，品类口径准）；insight-cube 的 raw.posts 直查作为灵活补充（任意关键词、任意时间窗）。
- **达人筛选/投放选人**：走达人精选的五平台 KOL 搜索 + kol.detail；insight-cube 的 hot.user 适合"品牌相关活跃达人"的舆情视角。
- **品牌舆情 BI**：聆媒洞察的统计家族（声量/情感/画像/趋势/热词）。
