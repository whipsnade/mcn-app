# DataTap MCP 工具说明（catalog 快照）

来源：开发库 `mcp_tool_catalog`——运行时生效的审核版 schema。input_schema 为该工具入参的 JSON Schema（KOL 搜索的入参需包在 request 对象内）。

## bilibili-mcp

### datatap.bilibili.general.search.v1

- 状态：approved / enabled=True
- 说明：B站内容关键词搜索

```json
{
  "type": "object",
  "required": [
    "keyword"
  ],
  "properties": {
    "page": {
      "type": "integer",
      "default": 1
    },
    "keyword": {
      "type": "string"
    }
  }
}
```

### datatap.bilibili.precise.results.v1

- 状态：approved / enabled=True
- 说明：B站精确搜索结果

```json
{
  "type": "object",
  "required": [
    "keyword"
  ],
  "properties": {
    "keyword": {
      "type": "string"
    },
    "search_type": {
      "type": "string",
      "default": "user"
    }
  }
}
```

### datatap.bilibili.search.user.v1

- 状态：approved / enabled=True
- 说明：B站用户搜索

```json
{
  "type": "object",
  "required": [
    "keyword"
  ],
  "properties": {
    "page": {
      "type": "integer",
      "default": 1
    },
    "keyword": {
      "type": "string"
    }
  }
}
```

### datatap.bilibili.video.danmaku.v1

- 状态：approved / enabled=True
- 说明：B站视频弹幕数据

```json
{
  "type": "object",
  "required": [
    "bvid"
  ],
  "properties": {
    "bvid": {
      "type": "string"
    }
  }
}
```

## insight-cube-mcp

### datatap.insight.analysis.target.search.v1

- 状态：approved / enabled=True
- 说明：分析对象规则检索

```json
{
  "type": "object",
  "title": "analysis_target_searchArguments",
  "properties": {
    "size": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Size",
      "default": 10,
      "description": "返回结果数量，默认10，最大100"
    },
    "analysis_target_id": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Id",
      "default": null,
      "description": "查询的分析对象ID"
    },
    "analysis_target_name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Name",
      "default": null,
      "description": "查询的分析对象名称，可以进行模糊匹配查询"
    }
  }
}
```

### datatap.insight.match.best.tag.v1

- 状态：approved / enabled=True
- 说明：品牌与品类标准标签匹配

```json
{
  "type": "object",
  "title": "match_best_tagArguments",
  "required": [
    "tag_type",
    "tag_names"
  ],
  "properties": {
    "tag_type": {
      "type": "string",
      "title": "Tag Type",
      "description": "标签类型。仅支持:'品牌标签'或'品类标签'"
    },
    "tag_names": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "title": "Tag Names",
      "description": "标签名称列表，用于智能匹配同一实体的不同表达形式。\n位置顺序：[原始关键词, 表达变体...] - 优先匹配原始关键词\n\n✅ 允许的变体：中英文名称、书写变体、官方全称/简称\n  示例: ['兰蔻','LANCOME','Lancôme']\n  示例: ['乳品','乳类产品']\n\n❌ 禁止的扩展：相关概念、近义词、上下级概念\n  错误: ['酸奶','乳品'] ❌  (包含上级概念)"
    },
    "requirement_desc": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Requirement Desc",
      "default": "",
      "description": "需求描述(可选)，用于匹配消歧。\n当标签存在歧义时(如'苹果'可指品牌/水果)，通过需求上下文辅助判断正确标签。\n示例: '分析苹果手机在小红书的讨论' → 匹配到'苹果(电子品牌)'"
    }
  }
}
```

### datatap.insight.query.analysis.v1

- 状态：approved / enabled=True
- 说明：品牌声量、互动、情感和平台维度统计

```json
{
  "type": "object",
  "title": "query_analysis_dataArguments",
  "required": [
    "target_type",
    "start_time",
    "end_time",
    "metrics",
    "datasource"
  ],
  "properties": {
    "anys": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "array",
            "items": {
              "type": "string"
            }
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Anys",
      "default": null,
      "description": "包含关键词组(keyword类型专用,必填)。\n**使用条件**: 当 target_type='keyword' 时，此参数为必填，否则调用失败。\n**规则:** 最多2组,组内OR关系,组间AND关系\n**示例:** [['麦当劳','McDonald','金拱门'],['汉堡','薯条']] 表示 (麦当劳 OR McDonald OR 金拱门) AND (汉堡 OR 薯条)\n"
    },
    "name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Name",
      "default": null,
      "description": "名称(keyword/tag类型必填)。keyword类型为圈数名称,tag类型为标签名称"
    },
    "outs": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Outs",
      "default": null,
      "description": "排除关键词列表(keyword类型可选)。示例:['假货','仿品']"
    },
    "filters": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Filters",
      "default": null,
      "description": "过滤条件字符串列表。格式: \"key__op__value\" 或 \"key__op\"(value为null时)\n\n**查询字段(key):** 为支持过滤的所有字段列表/自定义标签（自定义维度）\n\n**操作符(op):**\n- 等于/不等于: 精确匹配,支持多值OR。用于固定选项字段\n- 存在/不存在: 判断字段有无数据,value为null\n- 包含/排除: 文本搜索,支持多词OR。仅用于文本字段(话题、昵称等)\n- 大于/大于等于/小于/小于等于: 数值比较。仅用于数值字段\n\n**value类型前缀:**\n- 字符串: \"品牌标签__等于__小米\"\n- 列表: \"品牌标签__等于__list:小米,huawei\"\n- 整数: \"粉丝数__大于__int:1000\"\n- 浮点数: \"情感指数__大于等于__float:0.5\"\n- null: \"品牌标签__存在\"(省略value)\n\n**特殊字段:**\n- 帖子链接: 支持URL自动转换(微博、抖音、小红书)\n- 原帖链接: 支持URL自动转换(微博、抖音、小红书)\n\n**自定义维度过滤格式**\n自定义标签:{标签/维度ID}__等于__{标签值/维度值/规则ID}\n\n**示例:** [\"噪音标签__等于__否\", \"内容情感__等于__list:正面,中性\", \"用户粉丝数__大于__int:10000\", \"品牌标签__存在\", \"自定义标签:3456__等于__4567\"]\n"
    },
    "metrics": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "title": "Metrics",
      "description": "指标列表（必填），用于指定要计算的统计指标。\n\n**可用指标：**\n- **用户粉丝数**: 用户的粉丝数量（数值类型）\n- **声量**: 帖子数量，反映内容发布量\n- **发帖数**: 帖子数量，反映内容发布量\n- **互动数**: 总互动量\n- **情感指数**: 正负面情感综合得分，范围-100到100，反映舆论倾向\n- **用户数**: 去重用户数量，反映触达人群规模\n- **阅读数**: 内容的阅读/播放次数\n- **曝光量**: 内容的曝光次数\n- **评论数**: 内容的评论数量\n- **收藏数**: 内容的收藏数量\n- **点赞数**: 内容的点赞数量\n- **转发数**: 内容的转发/分享数量\n\n**示例：**\n[\"声量\", \"互动数\"], [\"声量\", \"互动数\", \"情感指数\", \"用户数\"]"
    },
    "end_time": {
      "type": "string",
      "title": "End Time",
      "description": "查询截止时间（必填，含当前时间），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-04-17 23:59:59'）\n- 仅日期: YYYY-MM-DD（例如'2025-04-17'，自动补全为 23:59:59）\n注意：时间范围最大支持一年"
    },
    "tag_type": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Tag Type",
      "default": null,
      "description": "标签类型(tag类型专用,必填)。可选值:'品牌标签'或'品类标签' **使用条件**: 当 target_type='tag' 时，此参数为必填，否则调用失败。"
    },
    "datasource": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "title": "Datasource",
      "description": "数据源字符串列表(必填)。格式: \"platform__source1,source2\" 或 \"platform\"(查所有站点)\n**示例:** [\"小红书\", \"短视频__抖音\", \"微博\"]\n"
    },
    "dimensions": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Dimensions",
      "default": null,
      "description": "维度列表（可选），用于对数据进行分组统计。**重要：最多支持 3 个维度**。\n\n**当前支持的维度（共42个，按分类组织）：**\n  - 内容维度：内容情感、内容主题、内容类型、数据类型、IP标签、原帖ID\n  - 商单维度：是否商单、合作品牌、提及品牌、是否投放广告、商单所属行业\n  - 地域维度：用户国家、用户省份、用户城市、用户城市等级\n  - 平台维度：平台、话题\n  - 时间维度：月、周、日、天\n  - 标签维度：品牌标签、品类标签、品类角度\n  - 用户维度：用户昵称、用户圈层标签、用户分类、用户性别、用户年龄组、用户ID、是否商业化达人、达人类型标签、达人层级\n  - 美妆细分标签：美妆成分标签、美妆功效标签、美妆场景标签、美妆包装设计标签、美妆肌肤类型标签、美妆头发类型标签、美妆问题痛点标签、美妆香味类型标签、美妆产品标签\n\n**示例：**\n[\"平台\", \"内容情感\"]（两个维度的交叉分析）\n[\"平台\"]（单维度统计）\n[] 或 null（无维度，返回整体概览数据）"
    },
    "field_name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Field Name",
      "default": null,
      "description": "字段名称(field类型专用,必填)。可选值:'内容ID'、'用户ID'、'用户昵称'、'帖子链接'、'原帖ID'、'原帖内容'、'原帖链接'\n\n**使用条件**: 当 target_type='field' 时，此参数为必填，否则调用失败。\n\n**关键区分逻辑（根据用户需求选择正确的字段）：**\n\n1. **查询帖子本身** (用户话术中**没有**\"评论\"关键词)：\n   - 使用 `内容ID` 或 `帖子链接`\n   - 示例场景：\n     - \"查询这个链接的帖子\" → 使用 `帖子链接`\n     - \"查询内容ID=123的帖子\" → 使用 `内容ID`\n     - \"这个用户发了什么帖子\" → 使用 `用户ID` 或 `用户昵称`\n\n2. **查询评论数据** (用户话术中**包含**\"评论\"关键词)：\n   - 使用 `原帖ID`、`原帖内容` 或 `原帖链接`\n   - 示例场景：\n     - \"查询这个链接帖子的评论\" → 使用 `原帖链接`\n     - \"查询这个帖子下面的评论\" → 使用 `原帖链接`\n     - \"查询内容ID=123的帖子的评论\" → 使用 `原帖ID`\n     - \"查询包含'好用'内容的主帖的评论\" → 使用 `原帖内容`\n\n**字段说明：**\n- `内容ID`: 帖子的唯一标识ID（查询帖子本身）\n- `帖子链接`: 帖子的URL链接（查询帖子本身，支持微博/抖音/小红书，自动转换）\n- `原帖ID`: 评论所属主帖的ID（查询评论数据）\n- `原帖链接`: 评论所属主帖的URL链接（查询评论数据，支持微博/抖音/小红书，自动转换）\n- `原帖内容`: 评论所属主帖的内容文本（查询评论数据，支持包含逻辑）\n- `用户ID`: 发帖用户的ID\n- `用户昵称`: 发帖用户的昵称\n"
    },
    "start_time": {
      "type": "string",
      "title": "Start Time",
      "description": "查询起始时间（必填），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-01-01 00:00:00'）\n- 仅日期: YYYY-MM-DD（例如'2025-01-01'，自动补全为 00:00:00）\n注意：时间范围最大支持一年"
    },
    "field_value": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Field Value",
      "default": null,
      "description": "字段值(field类型专用,必填)。支持多值列表(多个值OR关系)。**使用条件**: 当 target_type='field' 时，此参数为必填，否则调用失败。示例:['小米汽车'] 或 ['小米汽车','比亚迪']"
    },
    "target_type": {
      "enum": [
        "tag",
        "keyword",
        "analysisTargetRule",
        "field"
      ],
      "type": "string",
      "title": "Target Type",
      "description": "分析对象类型,圈定数据范围的方式。\n\n**类型选择:**\n- keyword: 主题监测/舆情分析(语义宽泛,适合探索性分析)\n- tag: 品牌/品类研究(有明确品牌或品类时优先,数据更精准)\n- analysisTargetRule: 使用预配置的分析对象规则（注：需已在 DataTap 令牌设置中配置魔方Pro系统凭证，即已购买魔方Pro系统，方可使用）\n- field: 指定字段精确查询(根据ID/昵称/链接等精确定位)\n"
    },
    "search_fields": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Search Fields",
      "default": null,
      "description": "关键词搜索范围，仅在 target_type=\"keyword\" 时生效，不传时默认搜索标题和正文。\n\n**⚠️ 重要：当用户明确要求搜索ASR/OCR等指定字段时，必须显式传递此参数！**\n\n**可选值**:\n- title: 标题\n- content: 内容正文\n- video_asr: 视频语音识别（ASR，含视频的帖子）\n- video_ocr: 视频文字识别（视频截帧 OCR，含视频的帖子）\n- screenshot: 图片文字识别（图片 OCR，含图片的帖子）\n\n**示例:**\n- 搜索标题+ASR+OCR: [\"title\", \"video_asr\", \"video_ocr\", \"screenshot\"]\n\n**用户需求映射指南：**\n- \"视频中提到\" → 包含 video_asr 和 video_ocr\n- \"画面/字幕/文字出现\" → 包含 video_ocr 和 screenshot\n- \"asr/ocr中包含\" → 包含 video_asr、video_ocr 和 screenshot\n"
    },
    "analysis_target_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Id",
      "default": null,
      "description": "分析对象规则ID(analysisTargetRule类型专用,必填,必须为数字格式) **使用条件**: 当 target_type='analysisTargetRule' 时，此参数为必填，否则调用失败。"
    },
    "analysis_target_name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Name",
      "default": null,
      "description": "分析对象规则名称(analysisTargetRule类型可选)。注意:这是真实标识规则的名称,如果用户没有明确提供,请留空,不要随便生成"
    }
  }
}
```

### datatap.insight.query.rank.list.v1

- 状态：approved / enabled=True
- 说明：社媒榜单数据查询

```json
{
  "type": "object",
  "title": "query_rank_listArguments",
  "required": [
    "source",
    "start_time",
    "end_time"
  ],
  "properties": {
    "size": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Size",
      "default": 100,
      "description": "返回数量(可选),默认100条"
    },
    "source": {
      "type": "string",
      "title": "Source",
      "description": "站点名称(必填)。支持:微博、抖音、知乎、百度"
    },
    "keyword": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Keyword",
      "default": null,
      "description": "关键词(可选),筛选包含特定词的话题。例:'华为'、'美妆'"
    },
    "end_time": {
      "type": "string",
      "title": "End Time",
      "description": "查询截止时间（必填，含当前时间），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-04-17 23:59:59'）\n- 仅日期: YYYY-MM-DD（例如'2025-04-17'，自动补全为 23:59:59）\n注意：时间范围最大支持一年"
    },
    "start_time": {
      "type": "string",
      "title": "Start Time",
      "description": "查询起始时间（必填），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-01-01 00:00:00'）\n- 仅日期: YYYY-MM-DD（例如'2025-01-01'，自动补全为 00:00:00）\n注意：时间范围最大支持一年"
    }
  }
}
```

### datatap.insight.query.raw.posts.v1

- 状态：approved / enabled=True
- 说明：社媒原帖明细检索

```json
{
  "type": "object",
  "title": "query_raw_postsArguments",
  "required": [
    "target_type",
    "start_time",
    "end_time",
    "datasource"
  ],
  "properties": {
    "anys": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "array",
            "items": {
              "type": "string"
            }
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Anys",
      "default": null,
      "description": "包含关键词组(keyword类型专用,必填)。\n**使用条件**: 当 target_type='keyword' 时，此参数为必填，否则调用失败。\n**规则:** 最多2组,组内OR关系,组间AND关系\n**示例:** [['麦当劳','McDonald','金拱门'],['汉堡','薯条']] 表示 (麦当劳 OR McDonald OR 金拱门) AND (汉堡 OR 薯条)\n"
    },
    "name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Name",
      "default": null,
      "description": "名称(keyword/tag类型必填)。keyword类型为圈数名称,tag类型为标签名称"
    },
    "outs": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Outs",
      "default": null,
      "description": "排除关键词列表(keyword类型可选)。示例:['假货','仿品']"
    },
    "size": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Size",
      "default": 100,
      "description": "返回结果数量，默认100条，**最大10000条**。建议：小众主题100-200条，高频话题300-500条，深度分析可增至1000条以上。对比分析时必须使用相同的size确保可比性"
    },
    "filters": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Filters",
      "default": null,
      "description": "过滤条件字符串列表。格式: \"key__op__value\" 或 \"key__op\"(value为null时)\n\n**查询字段(key):** 为支持过滤的所有字段列表/自定义标签（自定义维度）\n\n**操作符(op):**\n- 等于/不等于: 精确匹配,支持多值OR。用于固定选项字段\n- 存在/不存在: 判断字段有无数据,value为null\n- 包含/排除: 文本搜索,支持多词OR。仅用于文本字段(话题、昵称等)\n- 大于/大于等于/小于/小于等于: 数值比较。仅用于数值字段\n\n**value类型前缀:**\n- 字符串: \"品牌标签__等于__小米\"\n- 列表: \"品牌标签__等于__list:小米,huawei\"\n- 整数: \"粉丝数__大于__int:1000\"\n- 浮点数: \"情感指数__大于等于__float:0.5\"\n- null: \"品牌标签__存在\"(省略value)\n\n**特殊字段:**\n- 帖子链接: 支持URL自动转换(微博、抖音、小红书)\n- 原帖链接: 支持URL自动转换(微博、抖音、小红书)\n\n**自定义维度过滤格式**\n自定义标签:{标签/维度ID}__等于__{标签值/维度值/规则ID}\n\n**示例:** [\"噪音标签__等于__否\", \"内容情感__等于__list:正面,中性\", \"用户粉丝数__大于__int:10000\", \"品牌标签__存在\", \"自定义标签:3456__等于__4567\"]\n"
    },
    "end_time": {
      "type": "string",
      "title": "End Time",
      "description": "查询截止时间（必填，含当前时间），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-04-17 23:59:59'）\n- 仅日期: YYYY-MM-DD（例如'2025-04-17'，自动补全为 23:59:59）\n注意：时间范围最大支持一年"
    },
    "order_by": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Order By",
      "default": "综合",
      "description": "排序方式（倒序）,支持以下选项: 综合(默认)、发布时间、互动数、阅读数、曝光量"
    },
    "tag_type": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Tag Type",
      "default": null,
      "description": "标签类型(tag类型专用,必填)。可选值:'品牌标签'或'品类标签' **使用条件**: 当 target_type='tag' 时，此参数为必填，否则调用失败。"
    },
    "datasource": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "title": "Datasource",
      "description": "数据源字符串列表(必填)。格式: \"platform__source1,source2\" 或 \"platform\"(查所有站点)\n**示例:** [\"小红书\", \"短视频__抖音\", \"微博\"]\n"
    },
    "field_name": {
      "anyOf": [
        {
          "enum": [
            "内容ID",
            "用户ID",
            "用户昵称",
            "帖子链接",
            "原帖ID",
            "原帖内容",
            "原帖链接"
          ],
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Field Name",
      "default": null,
      "description": "字段名称(field类型专用,必填)。可选值:'内容ID'、'用户ID'、'用户昵称'、'帖子链接'、'原帖ID'、'原帖内容'、'原帖链接'\n\n**使用条件**: 当 target_type='field' 时，此参数为必填，否则调用失败。\n\n**关键区分逻辑（根据用户需求选择正确的字段）：**\n\n1. **查询帖子本身** (用户话术中**没有**\"评论\"关键词)：\n   - 使用 `内容ID` 或 `帖子链接`\n   - 示例场景：\n     - \"查询这个链接的帖子\" → 使用 `帖子链接`\n     - \"查询内容ID=123的帖子\" → 使用 `内容ID`\n     - \"这个用户发了什么帖子\" → 使用 `用户ID` 或 `用户昵称`\n\n2. **查询评论数据** (用户话术中**包含**\"评论\"关键词)：\n   - 使用 `原帖ID`、`原帖内容` 或 `原帖链接`\n   - 示例场景：\n     - \"查询这个链接帖子的评论\" → 使用 `原帖链接`\n     - \"查询这个帖子下面的评论\" → 使用 `原帖链接`\n     - \"查询内容ID=123的帖子的评论\" → 使用 `原帖ID`\n     - \"查询包含'好用'内容的主帖的评论\" → 使用 `原帖内容`\n\n**字段说明：**\n- `内容ID`: 帖子的唯一标识ID（查询帖子本身）\n- `帖子链接`: 帖子的URL链接（查询帖子本身，支持微博/抖音/小红书，自动转换）\n- `原帖ID`: 评论所属主帖的ID（查询评论数据）\n- `原帖链接`: 评论所属主帖的URL链接（查询评论数据，支持微博/抖音/小红书，自动转换）\n- `原帖内容`: 评论所属主帖的内容文本（查询评论数据，支持包含逻辑）\n- `用户ID`: 发帖用户的ID\n- `用户昵称`: 发帖用户的昵称\n"
    },
    "start_time": {
      "type": "string",
      "title": "Start Time",
      "description": "查询起始时间（必填），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-01-01 00:00:00'）\n- 仅日期: YYYY-MM-DD（例如'2025-01-01'，自动补全为 00:00:00）\n注意：时间范围最大支持一年"
    },
    "field_value": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Field Value",
      "default": null,
      "description": "字段值(field类型专用,必填)。支持多值列表(多个值OR关系)。**使用条件**: 当 target_type='field' 时，此参数为必填，否则调用失败。示例:['小米汽车'] 或 ['小米汽车','比亚迪']"
    },
    "target_type": {
      "enum": [
        "tag",
        "keyword",
        "analysisTargetRule",
        "field"
      ],
      "type": "string",
      "title": "Target Type",
      "description": "分析对象类型,圈定数据范围的方式。\n\n**类型选择:**\n- keyword: 主题监测/舆情分析(语义宽泛,适合探索性分析)\n- tag: 品牌/品类研究(有明确品牌或品类时优先,数据更精准)\n- analysisTargetRule: 使用预配置的分析对象规则（注：需已在 DataTap 令牌设置中配置魔方Pro系统凭证，即已购买魔方Pro系统，方可使用）\n- field: 指定字段精确查询(根据ID/昵称/链接等精确定位)\n"
    },
    "search_fields": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Search Fields",
      "default": null,
      "description": "关键词搜索范围，仅在 target_type=\"keyword\" 时生效，不传时默认搜索标题和正文。\n\n**⚠️ 重要：当用户明确要求搜索ASR/OCR等指定字段时，必须显式传递此参数！**\n\n**可选值**:\n- title: 标题\n- content: 内容正文\n- video_asr: 视频语音识别（ASR，含视频的帖子）\n- video_ocr: 视频文字识别（视频截帧 OCR，含视频的帖子）\n- screenshot: 图片文字识别（图片 OCR，含图片的帖子）\n\n**示例:**\n- 搜索标题+ASR+OCR: [\"title\", \"video_asr\", \"video_ocr\", \"screenshot\"]\n\n**用户需求映射指南：**\n- \"视频中提到\" → 包含 video_asr 和 video_ocr\n- \"画面/字幕/文字出现\" → 包含 video_ocr 和 screenshot\n- \"asr/ocr中包含\" → 包含 video_asr、video_ocr 和 screenshot\n"
    },
    "analysis_target_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Id",
      "default": null,
      "description": "分析对象规则ID(analysisTargetRule类型专用,必填,必须为数字格式) **使用条件**: 当 target_type='analysisTargetRule' 时，此参数为必填，否则调用失败。"
    },
    "analysis_target_name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Name",
      "default": null,
      "description": "分析对象规则名称(analysisTargetRule类型可选)。注意:这是真实标识规则的名称,如果用户没有明确提供,请留空,不要随便生成"
    }
  }
}
```

### datatap.insight.social.statistic.brand.activity.v1

- 状态：approved / enabled=True
- 说明：品牌相关活动列表与互动数据

```json
{
  "type": "object",
  "title": "social_statistic_brand_activityArguments",
  "required": [
    "start_time",
    "end_time",
    "datasource",
    "brand_name"
  ],
  "properties": {
    "end_time": {
      "type": "string",
      "title": "End Time",
      "description": "查询截止时间（必填，含当前时间），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-04-17 23:59:59'）\n- 仅日期: YYYY-MM-DD（例如'2025-04-17'，自动补全为 23:59:59）\n注意：时间范围最大支持一年"
    },
    "brand_name": {
      "type": "string",
      "title": "Brand Name",
      "description": "品牌标准名称或非标准别名"
    },
    "datasource": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "title": "Datasource",
      "description": "数据源字符串列表(必填)。格式: \"platform__source1,source2\" 或 \"platform\"(查所有站点)\n**示例:** [\"小红书\", \"短视频__抖音\", \"微博\"]\n"
    },
    "start_time": {
      "type": "string",
      "title": "Start Time",
      "description": "查询起始时间（必填），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-01-01 00:00:00'）\n- 仅日期: YYYY-MM-DD（例如'2025-01-01'，自动补全为 00:00:00）\n注意：时间范围最大支持一年"
    }
  }
}
```

### datatap.insight.social.statistic.category.rank.v1

- 状态：approved / enabled=True
- 说明：品类及子品类市场表现与声量排行

```json
{
  "type": "object",
  "title": "social_statistic_category_rankArguments",
  "required": [
    "category",
    "start_time",
    "end_time",
    "medias"
  ],
  "properties": {
    "medias": {
      "type": "array",
      "items": {
        "enum": [
          "小红书",
          "抖音"
        ],
        "type": "string"
      },
      "title": "Medias",
      "description": "品类榜的媒体列表，可选值：小红书、抖音"
    },
    "filters": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Filters",
      "default": null,
      "description": "过滤条件字符串列表。格式: \"key__op__value\" 或 \"key__op\"(value为null时)\n\n**查询字段(key):** 为支持过滤的所有字段列表/自定义标签（自定义维度）\n\n**操作符(op):**\n- 等于/不等于: 精确匹配,支持多值OR。用于固定选项字段\n- 存在/不存在: 判断字段有无数据,value为null\n- 包含/排除: 文本搜索,支持多词OR。仅用于文本字段(话题、昵称等)\n- 大于/大于等于/小于/小于等于: 数值比较。仅用于数值字段\n\n**value类型前缀:**\n- 字符串: \"品牌标签__等于__小米\"\n- 列表: \"品牌标签__等于__list:小米,huawei\"\n- 整数: \"粉丝数__大于__int:1000\"\n- 浮点数: \"情感指数__大于等于__float:0.5\"\n- null: \"品牌标签__存在\"(省略value)\n\n**特殊字段:**\n- 帖子链接: 支持URL自动转换(微博、抖音、小红书)\n- 原帖链接: 支持URL自动转换(微博、抖音、小红书)\n\n**自定义维度过滤格式**\n自定义标签:{标签/维度ID}__等于__{标签值/维度值/规则ID}\n\n**示例:** [\"噪音标签__等于__否\", \"内容情感__等于__list:正面,中性\", \"用户粉丝数__大于__int:10000\", \"品牌标签__存在\", \"自定义标签:3456__等于__4567\"]\n"
    },
    "category": {
      "type": "string",
      "title": "Category",
      "description": "\n品类名称，必须使用标准品类标签，格式为：一级品类(行业大类)-二级品类-三级品类（最多三级）。\n\n**获取方式**:\n1. 先使用 `match_best_tag` 工具（tag_type=\"品类标签\"）匹配标准品类名称\n2. 使用返回的标准品类名称填充此参数\n\n**支持的一级行业大类**:\n美妆护肤、个人护理、食品饮料、3C数码、汽车出行、母婴、酒类、家用电器、运动户外、服饰内衣、鞋靴箱包、家具家装、医疗保健、宠物用品\n\n**格式示例**:\n- 一级品类：\"食品饮料\"、\"美妆护肤\"\n- 二级品类：\"食品饮料-乳品\"、\"美妆护肤-面部护理\"\n- 三级品类：\"食品饮料-乳品-调制乳\"、\"美妆护肤-面部护理-卸妆\"\n\n**注意**: 必须使用 match_best_tag 返回的标准品类名称，不能自行拼接\n"
    },
    "end_time": {
      "type": "string",
      "title": "End Time",
      "description": "查询截止时间（必填，含当前时间），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-04-17 23:59:59'）\n- 仅日期: YYYY-MM-DD（例如'2025-04-17'，自动补全为 23:59:59）\n注意：时间范围最大支持一年"
    },
    "start_time": {
      "type": "string",
      "title": "Start Time",
      "description": "查询起始时间（必填），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-01-01 00:00:00'）\n- 仅日期: YYYY-MM-DD（例如'2025-01-01'，自动补全为 00:00:00）\n注意：时间范围最大支持一年"
    }
  }
}
```

### datatap.insight.social.statistic.hot.topic.v1

- 状态：approved / enabled=True
- 说明：品牌相关热门话题和声量聚类

```json
{
  "type": "object",
  "title": "social_statistic_hot_topicArguments",
  "required": [
    "target_type",
    "start_time",
    "end_time",
    "datasource"
  ],
  "properties": {
    "anys": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "array",
            "items": {
              "type": "string"
            }
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Anys",
      "default": null,
      "description": "包含关键词组(keyword类型专用,必填)。\n**使用条件**: 当 target_type='keyword' 时，此参数为必填，否则调用失败。\n**规则:** 最多2组,组内OR关系,组间AND关系\n**示例:** [['麦当劳','McDonald','金拱门'],['汉堡','薯条']] 表示 (麦当劳 OR McDonald OR 金拱门) AND (汉堡 OR 薯条)\n"
    },
    "name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Name",
      "default": null,
      "description": "名称(keyword/tag类型必填)。keyword类型为圈数名称,tag类型为标签名称"
    },
    "outs": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Outs",
      "default": null,
      "description": "排除关键词列表(keyword类型可选)。示例:['假货','仿品']"
    },
    "filters": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Filters",
      "default": null,
      "description": "过滤条件字符串列表。格式: \"key__op__value\" 或 \"key__op\"(value为null时)\n\n**查询字段(key):** 为支持过滤的所有字段列表/自定义标签（自定义维度）\n\n**操作符(op):**\n- 等于/不等于: 精确匹配,支持多值OR。用于固定选项字段\n- 存在/不存在: 判断字段有无数据,value为null\n- 包含/排除: 文本搜索,支持多词OR。仅用于文本字段(话题、昵称等)\n- 大于/大于等于/小于/小于等于: 数值比较。仅用于数值字段\n\n**value类型前缀:**\n- 字符串: \"品牌标签__等于__小米\"\n- 列表: \"品牌标签__等于__list:小米,huawei\"\n- 整数: \"粉丝数__大于__int:1000\"\n- 浮点数: \"情感指数__大于等于__float:0.5\"\n- null: \"品牌标签__存在\"(省略value)\n\n**特殊字段:**\n- 帖子链接: 支持URL自动转换(微博、抖音、小红书)\n- 原帖链接: 支持URL自动转换(微博、抖音、小红书)\n\n**自定义维度过滤格式**\n自定义标签:{标签/维度ID}__等于__{标签值/维度值/规则ID}\n\n**示例:** [\"噪音标签__等于__否\", \"内容情感__等于__list:正面,中性\", \"用户粉丝数__大于__int:10000\", \"品牌标签__存在\", \"自定义标签:3456__等于__4567\"]\n"
    },
    "end_time": {
      "type": "string",
      "title": "End Time",
      "description": "查询截止时间（必填，含当前时间），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-04-17 23:59:59'）\n- 仅日期: YYYY-MM-DD（例如'2025-04-17'，自动补全为 23:59:59）\n注意：时间范围最大支持一年"
    },
    "tag_type": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Tag Type",
      "default": null,
      "description": "标签类型(tag类型专用,必填)。可选值:'品牌标签'或'品类标签' **使用条件**: 当 target_type='tag' 时，此参数为必填，否则调用失败。"
    },
    "datasource": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "title": "Datasource",
      "description": "数据源字符串列表(必填)。格式: \"platform__source1,source2\" 或 \"platform\"(查所有站点)\n**示例:** [\"小红书\", \"短视频__抖音\", \"微博\"]\n"
    },
    "start_time": {
      "type": "string",
      "title": "Start Time",
      "description": "查询起始时间（必填），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-01-01 00:00:00'）\n- 仅日期: YYYY-MM-DD（例如'2025-01-01'，自动补全为 00:00:00）\n注意：时间范围最大支持一年"
    },
    "target_type": {
      "enum": [
        "tag",
        "keyword",
        "analysisTargetRule"
      ],
      "type": "string",
      "title": "Target Type",
      "description": "分析对象类型,圈定数据范围的方式。\n\n**类型选择:**\n- keyword: 主题监测/舆情分析(语义宽泛,适合探索性分析)\n- tag: 品牌/品类研究(有明确品牌或品类时优先,数据更精准)\n- analysisTargetRule: 使用预配置的分析对象规则（注：需已在 DataTap 令牌设置中配置魔方Pro系统凭证，即已购买魔方Pro系统，方可使用）\n"
    },
    "search_fields": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Search Fields",
      "default": null,
      "description": "关键词搜索范围，仅在 target_type=\"keyword\" 时生效，不传时默认搜索标题和正文。\n\n**⚠️ 重要：当用户明确要求搜索ASR/OCR等指定字段时，必须显式传递此参数！**\n\n**可选值**:\n- title: 标题\n- content: 内容正文\n- video_asr: 视频语音识别（ASR，含视频的帖子）\n- video_ocr: 视频文字识别（视频截帧 OCR，含视频的帖子）\n- screenshot: 图片文字识别（图片 OCR，含图片的帖子）\n\n**示例:**\n- 搜索标题+ASR+OCR: [\"title\", \"video_asr\", \"video_ocr\", \"screenshot\"]\n\n**用户需求映射指南：**\n- \"视频中提到\" → 包含 video_asr 和 video_ocr\n- \"画面/字幕/文字出现\" → 包含 video_ocr 和 screenshot\n- \"asr/ocr中包含\" → 包含 video_asr、video_ocr 和 screenshot\n"
    },
    "analysis_target_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Id",
      "default": null,
      "description": "分析对象规则ID(analysisTargetRule类型专用,必填,必须为数字格式) **使用条件**: 当 target_type='analysisTargetRule' 时，此参数为必填，否则调用失败。"
    },
    "analysis_target_name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Name",
      "default": null,
      "description": "分析对象规则名称(analysisTargetRule类型可选)。注意:这是真实标识规则的名称,如果用户没有明确提供,请留空,不要随便生成"
    }
  }
}
```

### datatap.insight.social.statistic.hot.user.v1

- 状态：approved / enabled=True
- 说明：品牌相关热门用户和传播达人

```json
{
  "type": "object",
  "title": "social_statistic_hot_userArguments",
  "required": [
    "target_type",
    "start_time",
    "end_time",
    "datasource"
  ],
  "properties": {
    "anys": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "array",
            "items": {
              "type": "string"
            }
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Anys",
      "default": null,
      "description": "包含关键词组(keyword类型专用,必填)。\n**使用条件**: 当 target_type='keyword' 时，此参数为必填，否则调用失败。\n**规则:** 最多2组,组内OR关系,组间AND关系\n**示例:** [['麦当劳','McDonald','金拱门'],['汉堡','薯条']] 表示 (麦当劳 OR McDonald OR 金拱门) AND (汉堡 OR 薯条)\n"
    },
    "name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Name",
      "default": null,
      "description": "名称(keyword/tag类型必填)。keyword类型为圈数名称,tag类型为标签名称"
    },
    "outs": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Outs",
      "default": null,
      "description": "排除关键词列表(keyword类型可选)。示例:['假货','仿品']"
    },
    "filters": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Filters",
      "default": null,
      "description": "过滤条件字符串列表。格式: \"key__op__value\" 或 \"key__op\"(value为null时)\n\n**查询字段(key):** 为支持过滤的所有字段列表/自定义标签（自定义维度）\n\n**操作符(op):**\n- 等于/不等于: 精确匹配,支持多值OR。用于固定选项字段\n- 存在/不存在: 判断字段有无数据,value为null\n- 包含/排除: 文本搜索,支持多词OR。仅用于文本字段(话题、昵称等)\n- 大于/大于等于/小于/小于等于: 数值比较。仅用于数值字段\n\n**value类型前缀:**\n- 字符串: \"品牌标签__等于__小米\"\n- 列表: \"品牌标签__等于__list:小米,huawei\"\n- 整数: \"粉丝数__大于__int:1000\"\n- 浮点数: \"情感指数__大于等于__float:0.5\"\n- null: \"品牌标签__存在\"(省略value)\n\n**特殊字段:**\n- 帖子链接: 支持URL自动转换(微博、抖音、小红书)\n- 原帖链接: 支持URL自动转换(微博、抖音、小红书)\n\n**自定义维度过滤格式**\n自定义标签:{标签/维度ID}__等于__{标签值/维度值/规则ID}\n\n**示例:** [\"噪音标签__等于__否\", \"内容情感__等于__list:正面,中性\", \"用户粉丝数__大于__int:10000\", \"品牌标签__存在\", \"自定义标签:3456__等于__4567\"]\n"
    },
    "sort_by": {
      "type": "string",
      "title": "Sort By",
      "default": "发帖数",
      "description": "排序字段（可选），用于指定热门用户的排序方式。\n\n**可用值:**\n- \"发帖数\"（默认）: 按发帖数排序\n- \"互动数\": 按互动数排序\n\n**示例:** \"发帖数\" 或 \"互动数\"\n"
    },
    "end_time": {
      "type": "string",
      "title": "End Time",
      "description": "查询截止时间（必填，含当前时间），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-04-17 23:59:59'）\n- 仅日期: YYYY-MM-DD（例如'2025-04-17'，自动补全为 23:59:59）\n注意：时间范围最大支持一年"
    },
    "tag_type": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Tag Type",
      "default": null,
      "description": "标签类型(tag类型专用,必填)。可选值:'品牌标签'或'品类标签' **使用条件**: 当 target_type='tag' 时，此参数为必填，否则调用失败。"
    },
    "datasource": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "title": "Datasource",
      "description": "数据源字符串列表(必填)。格式: \"platform__source1,source2\" 或 \"platform\"(查所有站点)\n**示例:** [\"小红书\", \"短视频__抖音\", \"微博\"]\n"
    },
    "start_time": {
      "type": "string",
      "title": "Start Time",
      "description": "查询起始时间（必填），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-01-01 00:00:00'）\n- 仅日期: YYYY-MM-DD（例如'2025-01-01'，自动补全为 00:00:00）\n注意：时间范围最大支持一年"
    },
    "target_type": {
      "enum": [
        "tag",
        "keyword",
        "analysisTargetRule"
      ],
      "type": "string",
      "title": "Target Type",
      "description": "分析对象类型,圈定数据范围的方式。\n\n**类型选择:**\n- keyword: 主题监测/舆情分析(语义宽泛,适合探索性分析)\n- tag: 品牌/品类研究(有明确品牌或品类时优先,数据更精准)\n- analysisTargetRule: 使用预配置的分析对象规则（注：需已在 DataTap 令牌设置中配置魔方Pro系统凭证，即已购买魔方Pro系统，方可使用）\n"
    },
    "search_fields": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Search Fields",
      "default": null,
      "description": "关键词搜索范围，仅在 target_type=\"keyword\" 时生效，不传时默认搜索标题和正文。\n\n**⚠️ 重要：当用户明确要求搜索ASR/OCR等指定字段时，必须显式传递此参数！**\n\n**可选值**:\n- title: 标题\n- content: 内容正文\n- video_asr: 视频语音识别（ASR，含视频的帖子）\n- video_ocr: 视频文字识别（视频截帧 OCR，含视频的帖子）\n- screenshot: 图片文字识别（图片 OCR，含图片的帖子）\n\n**示例:**\n- 搜索标题+ASR+OCR: [\"title\", \"video_asr\", \"video_ocr\", \"screenshot\"]\n\n**用户需求映射指南：**\n- \"视频中提到\" → 包含 video_asr 和 video_ocr\n- \"画面/字幕/文字出现\" → 包含 video_ocr 和 screenshot\n- \"asr/ocr中包含\" → 包含 video_asr、video_ocr 和 screenshot\n"
    },
    "analysis_target_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Id",
      "default": null,
      "description": "分析对象规则ID(analysisTargetRule类型专用,必填,必须为数字格式) **使用条件**: 当 target_type='analysisTargetRule' 时，此参数为必填，否则调用失败。"
    },
    "analysis_target_name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Name",
      "default": null,
      "description": "分析对象规则名称(analysisTargetRule类型可选)。注意:这是真实标识规则的名称,如果用户没有明确提供,请留空,不要随便生成"
    }
  }
}
```

### datatap.insight.social.statistic.overview.v1

- 状态：approved / enabled=True
- 说明：品牌或关键词社交搜索整体概览

```json
{
  "type": "object",
  "title": "social_statistic_overviewArguments",
  "required": [
    "target_type",
    "start_time",
    "end_time",
    "datasource"
  ],
  "properties": {
    "anys": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "array",
            "items": {
              "type": "string"
            }
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Anys",
      "default": null,
      "description": "包含关键词组(keyword类型专用,必填)。\n**使用条件**: 当 target_type='keyword' 时，此参数为必填，否则调用失败。\n**规则:** 最多2组,组内OR关系,组间AND关系\n**示例:** [['麦当劳','McDonald','金拱门'],['汉堡','薯条']] 表示 (麦当劳 OR McDonald OR 金拱门) AND (汉堡 OR 薯条)\n"
    },
    "name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Name",
      "default": null,
      "description": "名称(keyword/tag类型必填)。keyword类型为圈数名称,tag类型为标签名称"
    },
    "outs": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Outs",
      "default": null,
      "description": "排除关键词列表(keyword类型可选)。示例:['假货','仿品']"
    },
    "filters": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Filters",
      "default": null,
      "description": "过滤条件字符串列表。格式: \"key__op__value\" 或 \"key__op\"(value为null时)\n\n**查询字段(key):** 为支持过滤的所有字段列表/自定义标签（自定义维度）\n\n**操作符(op):**\n- 等于/不等于: 精确匹配,支持多值OR。用于固定选项字段\n- 存在/不存在: 判断字段有无数据,value为null\n- 包含/排除: 文本搜索,支持多词OR。仅用于文本字段(话题、昵称等)\n- 大于/大于等于/小于/小于等于: 数值比较。仅用于数值字段\n\n**value类型前缀:**\n- 字符串: \"品牌标签__等于__小米\"\n- 列表: \"品牌标签__等于__list:小米,huawei\"\n- 整数: \"粉丝数__大于__int:1000\"\n- 浮点数: \"情感指数__大于等于__float:0.5\"\n- null: \"品牌标签__存在\"(省略value)\n\n**特殊字段:**\n- 帖子链接: 支持URL自动转换(微博、抖音、小红书)\n- 原帖链接: 支持URL自动转换(微博、抖音、小红书)\n\n**自定义维度过滤格式**\n自定义标签:{标签/维度ID}__等于__{标签值/维度值/规则ID}\n\n**示例:** [\"噪音标签__等于__否\", \"内容情感__等于__list:正面,中性\", \"用户粉丝数__大于__int:10000\", \"品牌标签__存在\", \"自定义标签:3456__等于__4567\"]\n"
    },
    "end_time": {
      "type": "string",
      "title": "End Time",
      "description": "查询截止时间（必填，含当前时间），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-04-17 23:59:59'）\n- 仅日期: YYYY-MM-DD（例如'2025-04-17'，自动补全为 23:59:59）\n注意：时间范围最大支持一年"
    },
    "tag_type": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Tag Type",
      "default": null,
      "description": "标签类型(tag类型专用,必填)。可选值:'品牌标签'或'品类标签' **使用条件**: 当 target_type='tag' 时，此参数为必填，否则调用失败。"
    },
    "datasource": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "title": "Datasource",
      "description": "数据源字符串列表(必填)。格式: \"platform__source1,source2\" 或 \"platform\"(查所有站点)\n**示例:** [\"小红书\", \"短视频__抖音\", \"微博\"]\n"
    },
    "is_compare": {
      "type": "boolean",
      "title": "Is Compare",
      "default": true,
      "description": "是否进行对比分析，默认为True"
    },
    "start_time": {
      "type": "string",
      "title": "Start Time",
      "description": "查询起始时间（必填），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-01-01 00:00:00'）\n- 仅日期: YYYY-MM-DD（例如'2025-01-01'，自动补全为 00:00:00）\n注意：时间范围最大支持一年"
    },
    "target_type": {
      "enum": [
        "tag",
        "keyword",
        "analysisTargetRule"
      ],
      "type": "string",
      "title": "Target Type",
      "description": "分析对象类型,圈定数据范围的方式。\n\n**类型选择:**\n- keyword: 主题监测/舆情分析(语义宽泛,适合探索性分析)\n- tag: 品牌/品类研究(有明确品牌或品类时优先,数据更精准)\n- analysisTargetRule: 使用预配置的分析对象规则（注：需已在 DataTap 令牌设置中配置魔方Pro系统凭证，即已购买魔方Pro系统，方可使用）\n"
    },
    "search_fields": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Search Fields",
      "default": null,
      "description": "关键词搜索范围，仅在 target_type=\"keyword\" 时生效，不传时默认搜索标题和正文。\n\n**⚠️ 重要：当用户明确要求搜索ASR/OCR等指定字段时，必须显式传递此参数！**\n\n**可选值**:\n- title: 标题\n- content: 内容正文\n- video_asr: 视频语音识别（ASR，含视频的帖子）\n- video_ocr: 视频文字识别（视频截帧 OCR，含视频的帖子）\n- screenshot: 图片文字识别（图片 OCR，含图片的帖子）\n\n**示例:**\n- 搜索标题+ASR+OCR: [\"title\", \"video_asr\", \"video_ocr\", \"screenshot\"]\n\n**用户需求映射指南：**\n- \"视频中提到\" → 包含 video_asr 和 video_ocr\n- \"画面/字幕/文字出现\" → 包含 video_ocr 和 screenshot\n- \"asr/ocr中包含\" → 包含 video_asr、video_ocr 和 screenshot\n"
    },
    "analysis_target_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Id",
      "default": null,
      "description": "分析对象规则ID(analysisTargetRule类型专用,必填,必须为数字格式) **使用条件**: 当 target_type='analysisTargetRule' 时，此参数为必填，否则调用失败。"
    },
    "analysis_target_name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Name",
      "default": null,
      "description": "分析对象规则名称(analysisTargetRule类型可选)。注意:这是真实标识规则的名称,如果用户没有明确提供,请留空,不要随便生成"
    }
  }
}
```

### datatap.insight.social.statistic.trend.v1

- 状态：approved / enabled=True
- 说明：品牌或关键词跨平台声量趋势

```json
{
  "type": "object",
  "title": "social_statistic_trendArguments",
  "required": [
    "target_type",
    "start_time",
    "end_time",
    "datasource"
  ],
  "properties": {
    "anys": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "array",
            "items": {
              "type": "string"
            }
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Anys",
      "default": null,
      "description": "包含关键词组(keyword类型专用,必填)。\n**使用条件**: 当 target_type='keyword' 时，此参数为必填，否则调用失败。\n**规则:** 最多2组,组内OR关系,组间AND关系\n**示例:** [['麦当劳','McDonald','金拱门'],['汉堡','薯条']] 表示 (麦当劳 OR McDonald OR 金拱门) AND (汉堡 OR 薯条)\n"
    },
    "name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Name",
      "default": null,
      "description": "名称(keyword/tag类型必填)。keyword类型为圈数名称,tag类型为标签名称"
    },
    "outs": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Outs",
      "default": null,
      "description": "排除关键词列表(keyword类型可选)。示例:['假货','仿品']"
    },
    "filters": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Filters",
      "default": null,
      "description": "过滤条件字符串列表。格式: \"key__op__value\" 或 \"key__op\"(value为null时)\n\n**查询字段(key):** 为支持过滤的所有字段列表/自定义标签（自定义维度）\n\n**操作符(op):**\n- 等于/不等于: 精确匹配,支持多值OR。用于固定选项字段\n- 存在/不存在: 判断字段有无数据,value为null\n- 包含/排除: 文本搜索,支持多词OR。仅用于文本字段(话题、昵称等)\n- 大于/大于等于/小于/小于等于: 数值比较。仅用于数值字段\n\n**value类型前缀:**\n- 字符串: \"品牌标签__等于__小米\"\n- 列表: \"品牌标签__等于__list:小米,huawei\"\n- 整数: \"粉丝数__大于__int:1000\"\n- 浮点数: \"情感指数__大于等于__float:0.5\"\n- null: \"品牌标签__存在\"(省略value)\n\n**特殊字段:**\n- 帖子链接: 支持URL自动转换(微博、抖音、小红书)\n- 原帖链接: 支持URL自动转换(微博、抖音、小红书)\n\n**自定义维度过滤格式**\n自定义标签:{标签/维度ID}__等于__{标签值/维度值/规则ID}\n\n**示例:** [\"噪音标签__等于__否\", \"内容情感__等于__list:正面,中性\", \"用户粉丝数__大于__int:10000\", \"品牌标签__存在\", \"自定义标签:3456__等于__4567\"]\n"
    },
    "end_time": {
      "type": "string",
      "title": "End Time",
      "description": "查询截止时间（必填，含当前时间），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-04-17 23:59:59'）\n- 仅日期: YYYY-MM-DD（例如'2025-04-17'，自动补全为 23:59:59）\n注意：时间范围最大支持一年"
    },
    "tag_type": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Tag Type",
      "default": null,
      "description": "标签类型(tag类型专用,必填)。可选值:'品牌标签'或'品类标签' **使用条件**: 当 target_type='tag' 时，此参数为必填，否则调用失败。"
    },
    "dimension": {
      "anyOf": [
        {
          "enum": [
            "hour",
            "date",
            "week",
            "month"
          ],
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Dimension",
      "default": null,
      "description": "时间维度（可选）。不指定时自动根据时间范围选择：48小时内=hour，1个月内=date，3个月内=week，3个月以上=month\n\n**可选值**:\n- hour: 按小时聚合\n- date: 按天聚合\n- week: 按周聚合\n- month: 按月聚合\n\n**自动选择规则** (用户未明确指定时):\n- 时间跨度 ≤ 48小时 → 使用 hour\n- 时间跨度 49小时-1个月 → 使用 date\n- 时间跨度 1-3个月 → 使用 week\n- 时间跨度 > 3个月 → 使用 month\n\n**注意**: 用户明确要求特定粒度时（如\"按小时统计\"），以用户要求为准。\n"
    },
    "datasource": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "title": "Datasource",
      "description": "数据源字符串列表(必填)。格式: \"platform__source1,source2\" 或 \"platform\"(查所有站点)\n**示例:** [\"小红书\", \"短视频__抖音\", \"微博\"]\n"
    },
    "start_time": {
      "type": "string",
      "title": "Start Time",
      "description": "查询起始时间（必填），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-01-01 00:00:00'）\n- 仅日期: YYYY-MM-DD（例如'2025-01-01'，自动补全为 00:00:00）\n注意：时间范围最大支持一年"
    },
    "target_type": {
      "enum": [
        "tag",
        "keyword",
        "analysisTargetRule"
      ],
      "type": "string",
      "title": "Target Type",
      "description": "分析对象类型,圈定数据范围的方式。\n\n**类型选择:**\n- keyword: 主题监测/舆情分析(语义宽泛,适合探索性分析)\n- tag: 品牌/品类研究(有明确品牌或品类时优先,数据更精准)\n- analysisTargetRule: 使用预配置的分析对象规则（注：需已在 DataTap 令牌设置中配置魔方Pro系统凭证，即已购买魔方Pro系统，方可使用）\n"
    },
    "search_fields": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Search Fields",
      "default": null,
      "description": "关键词搜索范围，仅在 target_type=\"keyword\" 时生效，不传时默认搜索标题和正文。\n\n**⚠️ 重要：当用户明确要求搜索ASR/OCR等指定字段时，必须显式传递此参数！**\n\n**可选值**:\n- title: 标题\n- content: 内容正文\n- video_asr: 视频语音识别（ASR，含视频的帖子）\n- video_ocr: 视频文字识别（视频截帧 OCR，含视频的帖子）\n- screenshot: 图片文字识别（图片 OCR，含图片的帖子）\n\n**示例:**\n- 搜索标题+ASR+OCR: [\"title\", \"video_asr\", \"video_ocr\", \"screenshot\"]\n\n**用户需求映射指南：**\n- \"视频中提到\" → 包含 video_asr 和 video_ocr\n- \"画面/字幕/文字出现\" → 包含 video_ocr 和 screenshot\n- \"asr/ocr中包含\" → 包含 video_asr、video_ocr 和 screenshot\n"
    },
    "analysis_target_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Id",
      "default": null,
      "description": "分析对象规则ID(analysisTargetRule类型专用,必填,必须为数字格式) **使用条件**: 当 target_type='analysisTargetRule' 时，此参数为必填，否则调用失败。"
    },
    "analysis_target_name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Name",
      "default": null,
      "description": "分析对象规则名称(analysisTargetRule类型可选)。注意:这是真实标识规则的名称,如果用户没有明确提供,请留空,不要随便生成"
    }
  }
}
```

### datatap.insight.social.statistic.user.profile.v1

- 状态：approved / enabled=True
- 说明：品牌受众年龄、性别和地域画像

```json
{
  "type": "object",
  "title": "social_statistic_user_profileArguments",
  "required": [
    "target_type",
    "start_time",
    "end_time",
    "media"
  ],
  "properties": {
    "anys": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "array",
            "items": {
              "type": "string"
            }
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Anys",
      "default": null,
      "description": "包含关键词组(keyword类型专用,必填)。\n**使用条件**: 当 target_type='keyword' 时，此参数为必填，否则调用失败。\n**规则:** 最多2组,组内OR关系,组间AND关系\n**示例:** [['麦当劳','McDonald','金拱门'],['汉堡','薯条']] 表示 (麦当劳 OR McDonald OR 金拱门) AND (汉堡 OR 薯条)\n"
    },
    "name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Name",
      "default": null,
      "description": "名称(keyword/tag类型必填)。keyword类型为圈数名称,tag类型为标签名称"
    },
    "outs": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Outs",
      "default": null,
      "description": "排除关键词列表(keyword类型可选)。示例:['假货','仿品']"
    },
    "media": {
      "enum": [
        "小红书",
        "抖音",
        "微博"
      ],
      "type": "string",
      "title": "Media",
      "description": "人群所在媒体列表，可选值：小红书、抖音、微博"
    },
    "filters": {
      "anyOf": [
        {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        {
          "type": "null"
        }
      ],
      "title": "Filters",
      "default": null,
      "description": "过滤条件字符串列表。格式: \"key__op__value\" 或 \"key__op\"(value为null时)\n\n**查询字段(key):** 为支持过滤的所有字段列表/自定义标签（自定义维度）\n\n**操作符(op):**\n- 等于/不等于: 精确匹配,支持多值OR。用于固定选项字段\n- 存在/不存在: 判断字段有无数据,value为null\n- 包含/排除: 文本搜索,支持多词OR。仅用于文本字段(话题、昵称等)\n- 大于/大于等于/小于/小于等于: 数值比较。仅用于数值字段\n\n**value类型前缀:**\n- 字符串: \"品牌标签__等于__小米\"\n- 列表: \"品牌标签__等于__list:小米,huawei\"\n- 整数: \"粉丝数__大于__int:1000\"\n- 浮点数: \"情感指数__大于等于__float:0.5\"\n- null: \"品牌标签__存在\"(省略value)\n\n**特殊字段:**\n- 帖子链接: 支持URL自动转换(微博、抖音、小红书)\n- 原帖链接: 支持URL自动转换(微博、抖音、小红书)\n\n**自定义维度过滤格式**\n自定义标签:{标签/维度ID}__等于__{标签值/维度值/规则ID}\n\n**示例:** [\"噪音标签__等于__否\", \"内容情感__等于__list:正面,中性\", \"用户粉丝数__大于__int:10000\", \"品牌标签__存在\", \"自定义标签:3456__等于__4567\"]\n"
    },
    "end_time": {
      "type": "string",
      "title": "End Time",
      "description": "查询截止时间（必填，含当前时间），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-04-17 23:59:59'）\n- 仅日期: YYYY-MM-DD（例如'2025-04-17'，自动补全为 23:59:59）\n注意：时间范围最大支持一年"
    },
    "tag_type": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Tag Type",
      "default": null,
      "description": "标签类型(tag类型专用,必填)。可选值:'品牌标签'或'品类标签' **使用条件**: 当 target_type='tag' 时，此参数为必填，否则调用失败。"
    },
    "start_time": {
      "type": "string",
      "title": "Start Time",
      "description": "查询起始时间（必填），推荐使用 YYYY-MM-DD HH:MM:SS 格式，支持两种格式：\n- 完整时间（推荐）: YYYY-MM-DD HH:MM:SS（例如'2025-01-01 00:00:00'）\n- 仅日期: YYYY-MM-DD（例如'2025-01-01'，自动补全为 00:00:00）\n注意：时间范围最大支持一年"
    },
    "target_type": {
      "enum": [
        "tag",
        "keyword",
        "analysisTargetRule"
      ],
      "type": "string",
      "title": "Target Type",
      "description": "分析对象类型,圈定数据范围的方式。\n\n**类型选择:**\n- keyword: 主题监测/舆情分析(语义宽泛,适合探索性分析)\n- tag: 品牌/品类研究(有明确品牌或品类时优先,数据更精准)\n- analysisTargetRule: 使用预配置的分析对象规则（注：需已在 DataTap 令牌设置中配置魔方Pro系统凭证，即已购买魔方Pro系统，方可使用）\n"
    },
    "analysis_target_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Id",
      "default": null,
      "description": "分析对象规则ID(analysisTargetRule类型专用,必填,必须为数字格式) **使用条件**: 当 target_type='analysisTargetRule' 时，此参数为必填，否则调用失败。"
    },
    "analysis_target_name": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Analysis Target Name",
      "default": null,
      "description": "分析对象规则名称(analysisTargetRule类型可选)。注意:这是真实标识规则的名称,如果用户没有明确提供,请留空,不要随便生成"
    }
  }
}
```

## social-grow-mcp

### datatap.douyin.kol.search.v1

- 状态：approved / enabled=True
- 说明：抖音 KOL 候选检索。arguments 必须为 {request:{...}}；支持粉丝数、近月发帖量、女性和20～30岁受众、达人所在省份以及品牌关键词筛选。此工具仅能筛选达人所在地浙江省，不能验证粉丝城市为湖州/浙江或粉丝消费力金额，结果分析必须明确这些数据限制。

```json
{
  "type": "object",
  "required": [
    "request"
  ],
  "properties": {
    "request": {
      "anyOf": [
        {
          "type": "object",
          "properties": {
            "page": {
              "type": "integer",
              "minimum": 1
            },
            "size": {
              "type": "integer",
              "maximum": 100,
              "minimum": 1
            },
            "ageListFan": {
              "type": "array",
              "items": {
                "enum": [
                  "age1PercentFan",
                  "age2PercentFan",
                  "age3PercentFan",
                  "age4PercentFan",
                  "age5PercentFan",
                  "age6PercentFan"
                ],
                "type": "string"
              },
              "maxItems": 6
            },
            "sexListFan": {
              "type": "array",
              "items": {
                "enum": [
                  "malePercentFan",
                  "femalePercentFan"
                ],
                "type": "string"
              },
              "maxItems": 2
            },
            "sumpostMax": {
              "type": "integer",
              "minimum": 0
            },
            "sumpostMin": {
              "type": "integer",
              "minimum": 0
            },
            "ageListFanMin": {
              "type": "number",
              "maximum": 1,
              "minimum": 0
            },
            "sexListFanMin": {
              "type": "number",
              "maximum": 1,
              "minimum": 0
            },
            "kwProvinceList": {
              "type": "array",
              "items": {
                "type": "string"
              },
              "maxItems": 34
            },
            "textContentWord": {
              "type": "string",
              "maxLength": 100,
              "minLength": 1
            },
            "brandMentionsTag": {
              "type": "array",
              "items": {
                "type": "string"
              },
              "maxItems": 20
            },
            "followercountMax": {
              "type": "integer",
              "minimum": 0
            },
            "followercountMin": {
              "type": "integer",
              "minimum": 0
            },
            "categoryMentionsTag": {
              "type": "array",
              "items": {
                "type": "string"
              },
              "maxItems": 20
            }
          },
          "additionalProperties": false
        },
        {
          "type": "null"
        }
      ],
      "default": null
    }
  },
  "additionalProperties": false
}
```

### datatap.social.grow.kol.bilibili.search.v1

- 状态：approved / enabled=True
- 说明：B站 KOL 候选检索

```json
{
  "type": "object",
  "properties": {
    "request": {
      "anyOf": [
        {
          "type": "object",
          "properties": {
            "page": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": 1,
              "description": "页码 (默认=1)"
            },
            "size": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": 10,
              "description": "每页条数 (默认=10, 最大=100)"
            },
            "indexMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "综合评分-最大值"
            },
            "indexMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "综合评分-最小值"
            },
            "kwGender": {
              "anyOf": [
                {
                  "type": "string",
                  "oneOf": [
                    {
                      "const": "男",
                      "description": "男"
                    },
                    {
                      "const": "女",
                      "description": "女"
                    }
                  ],
                  "description": "性别"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "性别"
            },
            "nickname": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "昵称"
            },
            "verified": {
              "anyOf": [
                {
                  "type": "boolean"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "是否已认证"
            },
            "avgviewMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "平均播放-最大值"
            },
            "avgviewMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "平均播放-最小值"
            },
            "sexListFan": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string",
                    "oneOf": [
                      {
                        "const": "malePercentFan",
                        "description": "男"
                      },
                      {
                        "const": "femalePercentFan",
                        "description": "女"
                      }
                    ],
                    "description": "粉丝性别"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "[组合]受众性别占比-性别"
            },
            "sumpostMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "月发帖量-最大值"
            },
            "sumpostMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "月发帖量-最小值"
            },
            "postcountMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "作品数-最大值"
            },
            "postcountMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "作品数-最小值"
            },
            "sexListFanMin": {
              "anyOf": [
                {
                  "type": "number"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "[组合]受众性别占比-最小值"
            },
            "avginteractMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "平均互动-最大值"
            },
            "avginteractMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "平均互动-最小值"
            },
            "talentTypeLabel": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "其它标签"
            },
            "textContentWord": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "按发文内容中提及到的关键字进行筛选"
            },
            "brandMentionsTag": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "提及筛选-品牌标签"
            },
            "contentTypeLabel": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "达人类型标签"
            },
            "followercountMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "粉丝数-最大值"
            },
            "followercountMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "粉丝数-最小值"
            },
            "kolOfficialPriceL1Max": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "达人报价-最大值"
            },
            "kolOfficialPriceL1Min": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "达人报价-最小值"
            }
          },
          "description": "B站 KOL 列表查询请求"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "description": "查询请求参数"
    }
  },
  "additionalProperties": false
}
```

### datatap.social.grow.kol.class.tag.dictionary.v1

- 状态：approved / enabled=True
- 说明：KOL 分类标签字典

```json
{
  "type": "object",
  "required": [
    "platform"
  ],
  "properties": {
    "platform": {
      "type": "string",
      "oneOf": [
        {
          "const": "douyin",
          "description": "抖音"
        },
        {
          "const": "xiaohongshu",
          "description": "小红书"
        },
        {
          "const": "weibo",
          "description": "微博"
        },
        {
          "const": "wechat",
          "description": "微信"
        },
        {
          "const": "bilibili",
          "description": "B站"
        }
      ],
      "description": "平台"
    }
  },
  "additionalProperties": false
}
```

### datatap.social.grow.kol.detail.v1

- 状态：approved / enabled=True
- 说明：指定平台达人详情与趋势画像。scope 有效取值：accountTrend(账号趋势)、priceTrend(价格趋势)、postSummaryStatistics(发帖汇总)、postDailyStatistics(发帖分天)、hotWord(品牌热词)、fansAudience(受众画像)；抖音另有 businessXT/businessCar，小红书另有 businessPGY，各平台均有 businessBrand。不要自造 scope 取值。

```json
{
  "type": "object",
  "required": [
    "platform",
    "kwUidList",
    "scope"
  ],
  "properties": {
    "scope": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "description": "要查询的纬度集合"
    },
    "endDate": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "description": "时间范围-结束日期 (格式:yyyy-MM-dd)"
    },
    "platform": {
      "type": "string",
      "oneOf": [
        {
          "const": "douyin",
          "description": "抖音"
        },
        {
          "const": "xiaohongshu",
          "description": "小红书"
        },
        {
          "const": "weibo",
          "description": "微博"
        },
        {
          "const": "wechat",
          "description": "微信"
        },
        {
          "const": "bilibili",
          "description": "B站"
        }
      ],
      "description": "平台"
    },
    "kwUidList": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "description": "kwUid集合"
    },
    "startDate": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "description": "时间范围-开始日期 (格式:yyyy-MM-dd)"
    }
  },
  "additionalProperties": false
}
```

### datatap.social.grow.kol.match.mentions.tag.v1

- 状态：approved / enabled=True
- 说明：品牌提及标签匹配

```json
{
  "type": "object",
  "required": [
    "platform",
    "mentionsTagType",
    "keywords"
  ],
  "properties": {
    "keywords": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "description": "关键词"
    },
    "platform": {
      "type": "string",
      "oneOf": [
        {
          "const": "douyin",
          "description": "抖音"
        },
        {
          "const": "xiaohongshu",
          "description": "小红书"
        },
        {
          "const": "weibo",
          "description": "微博"
        },
        {
          "const": "wechat",
          "description": "微信"
        },
        {
          "const": "bilibili",
          "description": "B站"
        }
      ],
      "description": "平台"
    },
    "mentionsTagType": {
      "type": "integer",
      "oneOf": [
        {
          "const": 2001,
          "description": "提及筛选-品牌标签"
        },
        {
          "const": 2002,
          "description": "提及筛选-品类标签"
        }
      ],
      "description": "提及筛选标签"
    }
  },
  "additionalProperties": false
}
```

### datatap.social.grow.kol.wechat.search.v1

- 状态：approved / enabled=True
- 说明：微信 KOL 候选检索

```json
{
  "type": "object",
  "properties": {
    "request": {
      "anyOf": [
        {
          "type": "object",
          "properties": {
            "page": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": 1,
              "description": "页码 (默认=1)"
            },
            "size": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": 10,
              "description": "每页条数 (默认=10, 最大=100)"
            },
            "indexMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "综合评分-最大值"
            },
            "indexMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "综合评分-最小值"
            },
            "nickname": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "昵称"
            },
            "verified": {
              "anyOf": [
                {
                  "type": "boolean"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "是否已认证"
            },
            "cityLevel": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string",
                    "oneOf": [
                      {
                        "const": "一线城市",
                        "description": "一线城市"
                      },
                      {
                        "const": "新一线城市",
                        "description": "新一线城市"
                      },
                      {
                        "const": "二线城市",
                        "description": "二线城市"
                      },
                      {
                        "const": "三线及以下城市",
                        "description": "三线及以下城市"
                      }
                    ],
                    "description": "达人城市等级"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "达人城市级别"
            },
            "sumpostMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "月发帖量-最大值"
            },
            "sumpostMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "月发帖量-最小值"
            },
            "avginteractMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "平均互动-最大值"
            },
            "avginteractMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "平均互动-最小值"
            },
            "kwProvinceList": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "达人所在省份(含省、自治区、直辖市、行政区，使用标准全称)"
            },
            "textContentWord": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "按发文内容中提及到的关键字进行筛选"
            },
            "brandMentionsTag": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "提及筛选-品牌标签"
            },
            "contentTypeLabel": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "达人类型标签"
            },
            "followercountMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "粉丝数-最大值"
            },
            "followercountMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "粉丝数-最小值"
            },
            "kolOfficialPriceL1Max": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "头条报价-最大值"
            },
            "kolOfficialPriceL1Min": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "头条报价-最小值"
            },
            "kolOfficialPriceL2Max": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "次条报价-最大值"
            },
            "kolOfficialPriceL2Min": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "次条报价-最小值"
            },
            "kolOfficialPriceL3Max": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "3+条报价-最大值"
            },
            "kolOfficialPriceL3Min": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "3+条报价-最小值"
            }
          },
          "description": "微信 KOL 列表查询请求"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "description": "查询请求参数"
    }
  },
  "additionalProperties": false
}
```

### datatap.social.grow.kol.weibo.search.v1

- 状态：approved / enabled=True
- 说明：微博 KOL 候选检索

```json
{
  "type": "object",
  "properties": {
    "request": {
      "anyOf": [
        {
          "type": "object",
          "properties": {
            "page": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": 1,
              "description": "页码 (默认=1)"
            },
            "size": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": 10,
              "description": "每页条数 (默认=10, 最大=100)"
            },
            "ageMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "达人年龄-最大值"
            },
            "ageMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "达人年龄-最小值"
            },
            "indexMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "综合评分-最大值"
            },
            "indexMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "综合评分-最小值"
            },
            "kwGender": {
              "anyOf": [
                {
                  "type": "string",
                  "oneOf": [
                    {
                      "const": "男",
                      "description": "男"
                    },
                    {
                      "const": "女",
                      "description": "女"
                    }
                  ],
                  "description": "性别"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "性别"
            },
            "nickname": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "昵称"
            },
            "verified": {
              "anyOf": [
                {
                  "type": "boolean"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "是否已认证"
            },
            "cityLevel": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string",
                    "oneOf": [
                      {
                        "const": "一线城市",
                        "description": "一线城市"
                      },
                      {
                        "const": "新一线城市",
                        "description": "新一线城市"
                      },
                      {
                        "const": "二线城市",
                        "description": "二线城市"
                      },
                      {
                        "const": "三线及以下城市",
                        "description": "三线及以下城市"
                      }
                    ],
                    "description": "达人城市等级"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "达人城市级别"
            },
            "kwCountry": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "达人所在国家 (使用标准全称)"
            },
            "ageListFan": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string",
                    "oneOf": [
                      {
                        "const": "age1PercentFan",
                        "description": "18岁以下"
                      },
                      {
                        "const": "age2PercentFan",
                        "description": "18至24岁"
                      },
                      {
                        "const": "age3PercentFan",
                        "description": "25至29岁"
                      },
                      {
                        "const": "age4PercentFan",
                        "description": "30至34岁"
                      },
                      {
                        "const": "age5PercentFan",
                        "description": "35至39岁"
                      },
                      {
                        "const": "age6PercentFan",
                        "description": "40至49岁"
                      },
                      {
                        "const": "age7PercentFan",
                        "description": "50岁以上"
                      }
                    ],
                    "description": "年龄区间"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "[组合]受众年龄占比-年龄区间"
            },
            "sexListFan": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string",
                    "oneOf": [
                      {
                        "const": "malePercentFan",
                        "description": "男"
                      },
                      {
                        "const": "femalePercentFan",
                        "description": "女"
                      }
                    ],
                    "description": "粉丝性别"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "[组合]受众性别占比-性别"
            },
            "sumpostMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "月发帖量-最大值"
            },
            "sumpostMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "月发帖量-最小值"
            },
            "cityListFan": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string",
                    "oneOf": [
                      {
                        "const": "firstTierCity",
                        "description": "一线城市"
                      },
                      {
                        "const": "newFirstTierCity",
                        "description": "新一线城市"
                      },
                      {
                        "const": "secondTierCity",
                        "description": "二线城市"
                      },
                      {
                        "const": "thirdTierBelowCity",
                        "description": "三线及以下城市"
                      }
                    ],
                    "description": "粉丝城市等级"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "[组合]受众城市等级占比-城市等级"
            },
            "postcountMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "作品数-最大值"
            },
            "postcountMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "作品数-最小值"
            },
            "ageListFanMin": {
              "anyOf": [
                {
                  "type": "number"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "[组合]受众年龄占比-最小值"
            },
            "sexListFanMin": {
              "anyOf": [
                {
                  "type": "number"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "[组合]受众性别占比-最小值"
            },
            "avginteractMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "平均互动-最大值"
            },
            "avginteractMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "平均互动-最小值"
            },
            "cityListFanMin": {
              "anyOf": [
                {
                  "type": "number"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "[组合]受众城市等级占比-最小值"
            },
            "kwProvinceList": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "达人所在省份(含省、自治区、直辖市、行政区，使用标准全称)"
            },
            "talentTypeLabel": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "其它标签"
            },
            "textContentWord": {
              "anyOf": [
                {
                  "type": "string"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "按发文内容中提及到的关键字进行筛选"
            },
            "brandMentionsTag": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "提及筛选-品牌标签"
            },
            "contentTypeLabel": {
              "anyOf": [
                {
                  "type": "array",
                  "items": {
                    "type": "string"
                  }
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "达人类型标签"
            },
            "explosiveRateMax": {
              "anyOf": [
                {
                  "type": "number"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "爆文率-最大值"
            },
            "explosiveRateMin": {
              "anyOf": [
                {
                  "type": "number"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "爆文率-最小值"
            },
            "followercountMax": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "粉丝数-最大值"
            },
            "followercountMin": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "粉丝数-最小值"
            },
            "kolOfficialPriceL1Max": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "原创报价-最大值"
            },
            "kolOfficialPriceL1Min": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "原创报价-最小值"
            },
            "kolOfficialPriceL2Max": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "转发报价-最大值"
            },
            "kolOfficialPriceL2Min": {
              "anyOf": [
                {
                  "type": "integer"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "转发报价-最小值"
            },
            "realityfollowerrateMax": {
              "anyOf": [
                {
                  "type": "number"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "有效粉丝率-最大值"
            },
            "realityfollowerrateMin": {
              "anyOf": [
                {
                  "type": "number"
                },
                {
                  "type": "null"
                }
              ],
              "default": null,
              "description": "有效粉丝率-最小值"
            }
          },
          "description": "微博 KOL 列表查询请求"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "description": "查询请求参数"
    }
  },
  "additionalProperties": false
}
```

### datatap.xiaohongshu.kol.search.v1

- 状态：approved / enabled=True
- 说明：小红书 KOL 候选检索。arguments 必须为 {request:{...}}；优先按用户条件设置 page=1、size（最多100）、followercountMin、sumpostMin（近30天活跃）、sexListFan/sexListFanMin、ageListFan/ageListFanMin、kwProvinceList、品牌或品类提及标签，无法确定标签时才用 textContentWord。此工具仅能筛选达人所在地浙江省，不能验证粉丝城市为湖州/浙江或粉丝消费力金额，结果分析必须明确这些数据限制。

```json
{
  "type": "object",
  "required": [
    "request"
  ],
  "properties": {
    "request": {
      "anyOf": [
        {
          "type": "object",
          "properties": {
            "page": {
              "type": "integer",
              "minimum": 1
            },
            "size": {
              "type": "integer",
              "maximum": 100,
              "minimum": 1
            },
            "indexMax": {
              "type": "integer",
              "maximum": 100,
              "minimum": 0
            },
            "indexMin": {
              "type": "integer",
              "maximum": 100,
              "minimum": 0
            },
            "ageListFan": {
              "type": "array",
              "items": {
                "enum": [
                  "age1PercentFan",
                  "age2PercentFan",
                  "age3PercentFan",
                  "age4PercentFan",
                  "age5PercentFan"
                ],
                "type": "string"
              },
              "maxItems": 5
            },
            "sexListFan": {
              "type": "array",
              "items": {
                "enum": [
                  "malePercentFan",
                  "femalePercentFan"
                ],
                "type": "string"
              },
              "maxItems": 2
            },
            "sumpostMax": {
              "type": "integer",
              "minimum": 0
            },
            "sumpostMin": {
              "type": "integer",
              "minimum": 0
            },
            "ageListFanMin": {
              "type": "number",
              "maximum": 1,
              "minimum": 0
            },
            "sexListFanMin": {
              "type": "number",
              "maximum": 1,
              "minimum": 0
            },
            "avginteractMax": {
              "type": "integer",
              "minimum": 0
            },
            "avginteractMin": {
              "type": "integer",
              "minimum": 0
            },
            "kwProvinceList": {
              "type": "array",
              "items": {
                "type": "string"
              },
              "maxItems": 34
            },
            "textContentWord": {
              "type": "string",
              "maxLength": 100,
              "minLength": 1
            },
            "brandMentionsTag": {
              "type": "array",
              "items": {
                "type": "string"
              },
              "maxItems": 20
            },
            "explosiveRateMax": {
              "type": "number",
              "maximum": 1,
              "minimum": 0
            },
            "explosiveRateMin": {
              "type": "number",
              "maximum": 1,
              "minimum": 0
            },
            "followercountMax": {
              "type": "integer",
              "minimum": 0
            },
            "followercountMin": {
              "type": "integer",
              "minimum": 0
            },
            "categoryMentionsTag": {
              "type": "array",
              "items": {
                "type": "string"
              },
              "maxItems": 20
            },
            "realityfollowerrateMax": {
              "type": "number",
              "maximum": 1,
              "minimum": 0
            },
            "realityfollowerrateMin": {
              "type": "number",
              "maximum": 1,
              "minimum": 0
            }
          },
          "additionalProperties": false
        },
        {
          "type": "null"
        }
      ],
      "default": null
    }
  },
  "additionalProperties": false
}
```
