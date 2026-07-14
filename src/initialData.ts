import { Session } from "./types";

export const initialSessions: Session[] = [
  {
    id: "WO-1001",
    title: "完美日记-丝绒雾面系列发布",
    brand: "完美日记 (Perfect Diary)",
    campaignName: "丝绒雾面系列新品发布会",
    status: "completed",
    platform: "Xiaohongshu",
    category: "美妆护肤",
    targetAudience: "18-24 岁女性",
    budgetMin: "100000",
    budgetMax: "300000",
    isStarred: true,
    createdAt: "2026-07-10T10:00:00+08:00",
    updatedAt: "2026-07-10T10:07:00+08:00",
    mcn: "星耀互娱 (StarMCN)",
    kols: ["Alice Freeman (美妆头部)", "Diana Prince (美妆腰部)", "Jerry Wang (男士理容)"],
    summary: "美妆KOL矩阵推广，重点攻占小红书18-24岁女性心智，主打「丝绒质感、轻薄服帖、不沾杯」。",
    messages: [
      {
        id: "m1",
        sender: "user",
        text: "分析一下「完美日记丝绒雾面系列新品发布」的活动效果，特别是合作的几个KOL的数据和MCN的履约情况。",
        timestamp: "上午 10:00"
      },
      {
        id: "m2",
        sender: "ai",
        text: "收到！正在调取完美日记与合作MCN「星耀互娱」旗下3位KOL的联合推广数据进行分析。\n\n本次活动主要投放在 **小红书 (Xiaohongshu)**，以美妆护肤品类种草为主。整体履约效果良好，曝光率达大盘 **115%**。我已经完成了详尽的BI报告，呈现在右侧面板：\n\n- **正面情绪高涨 (78%)**：用户对「不沾杯」和「哑光质感」反馈最强烈。\n- **MCN履约率 (98%)**：合作MCN整体配合度极高，脚本过审快，且争取到了2篇爆文爆发。\n- **ROI达 2.45**：对于新品声量冷启动，该表现相当突出。\n\n请点击右侧的各图表切片查看人群画像和转化明细。您有任何特定达人的带货数据微调，也请随时告诉我！",
        timestamp: "上午 10:02"
      },
      {
        id: "m3",
        sender: "user",
        text: "其中 Alice Freeman 粉丝正向反馈很高，但是感觉她的转化成本偏高，能详细分析一下她的CPM和投流性价比吗？",
        timestamp: "上午 10:05"
      },
      {
        id: "m4",
        sender: "ai",
        text: "针对 **Alice Freeman (美妆头部)** 的深度剖析如下：\n\n1. **数据看板**：粉丝量 **2.5M**，单篇爆文点赞破 **45K**。虽然单次合作成本较高（¥85,000），但她带来的外溢检索量和直接进店转化达 **¥185,000**，直接 ROI 达 **2.18**。\n2. **投流性价比 (CPE)**：由于内容制作极其精良，该达人的千次曝光成本 (CPM) 为 **¥34**，甚至低于中尾部达人平均水平；单次互动成本 (CPE) 为 **¥1.8**，极具性价比。\n\n**分析结论**：虽然决策成本高，但她是本次活动的「大曝光破圈手」。建议后续在她的评论区增加「置顶购买链接」，并以信息流投放（薯条推广）延长其优质笔记的生命周期。",
        timestamp: "上午 10:07"
      }
    ],
    reportData: {
      sentiment: {
        positive: 78,
        neutral: 15,
        negative: 7,
        keywords: ["质感高级", "绝美雾面", "完全不干", "平替天花板", "稍微沾杯", "显白神器"]
      },
      engagement: {
        totalViews: 12500000,
        totalLikes: 620000,
        totalComments: 85000,
        totalShares: 45000,
        avgEngagementRate: 6.2,
        trendData: [
          { name: "6.20 (预热)", views: 350000, engagement: 2.8 },
          { name: "6.21 (发布)", views: 1800000, engagement: 5.4 },
          { name: "6.22 (高峰)", views: 4200000, engagement: 7.8 },
          { name: "6.23 (发酵)", views: 3100000, engagement: 6.9 },
          { name: "6.24 (稳步)", views: 1800000, engagement: 5.8 },
          { name: "6.25 (尾声)", views: 950000, engagement: 4.8 },
          { name: "6.26 (外溢)", views: 300000, engagement: 3.5 }
        ]
      },
      demographics: {
        gender: [
          { name: "女性 (Female)", value: 85, color: "#ec4899" },
          { name: "男性 (Male)", value: 15, color: "#3b82f6" }
        ],
        age: [
          { name: "18-24", value: 52 },
          { name: "25-30", value: 33 },
          { name: "31-35", value: 11 },
          { name: "36+", value: 4 }
        ],
        region: [
          { name: "广东", value: 22 },
          { name: "上海", value: 19 },
          { name: "北京", value: 16 },
          { name: "浙江", value: 14 },
          { name: "江苏", value: 11 },
          { name: "其他", value: 18 }
        ]
      },
      mcnAnalysis: {
        mcnName: "星耀互娱 (StarMCN)",
        fulfillmentRate: 98,
        cpm: 38,
        cpe: 1.8,
        roi: 2.45,
        score: 94,
        strengths: ["KOL配合度极高", "视频剪辑和脚本极具小红书网感", "自主追加站内搜索卡位投流"],
        weaknesses: ["头部网红档期冲突，沟通周期拉长", "腰部达人互动评论回复不够及时"]
      },
      kolPerformance: [
        {
          name: "Alice Freeman (头部)",
          avatar: "",
          platform: "Xiaohongshu",
          followers: "2.5M",
          engagementRate: 7.2,
          cost: "¥85,000",
          salesConversion: "¥185,000",
          sentimentPositive: 82
        },
        {
          name: "Diana Prince (腰部)",
          avatar: "",
          platform: "Xiaohongshu",
          followers: "850K",
          engagementRate: 5.4,
          cost: "¥32,000",
          salesConversion: "¥68,000",
          sentimentPositive: 76
        },
        {
          name: "Jerry Wang (男士理容)",
          avatar: "",
          platform: "Xiaohongshu",
          followers: "520K",
          engagementRate: 4.8,
          cost: "¥21,000",
          salesConversion: "¥38,000",
          sentimentPositive: 71
        }
      ],
      recommendations: [
        "1. **强化Alice Freeman追投**：在头部达人评论区中置顶「完美日记官方旗舰店」专属折扣链接，将声量瞬间转化为直接购买转化率。",
        "2. **复刻小红书爆文公式**：针对Diana Prince的『不沾杯测评』笔记追加薯条投放，复刻该脚本并在下一次活动中批量应用。",
        "3. **探索抖音短视频矩阵**：鉴于完美日记的视觉张力，建议第二阶段由星耀互娱配合，将优质视频二创剪辑，进行抖音全域覆盖。"
      ]
    }
  },
  {
    id: "WO-1002",
    title: "欧莱雅-玻尿酸复颜系列推广",
    brand: "巴黎欧莱雅 (L'Oreal)",
    campaignName: "玻尿酸复颜系列抗初老宣发",
    status: "completed",
    platform: "Douyin",
    category: "美妆护肤",
    targetAudience: "25-35 岁都市白领",
    budgetMin: "200000",
    budgetMax: "500000",
    isStarred: false,
    createdAt: "2026-07-09T09:00:00+08:00",
    updatedAt: "2026-07-09T09:05:00+08:00",
    mcn: "微念传媒 (Weinian)",
    kols: ["Elena Zhou (科技美妆)", "Sunny Chen (轻奢生活)"],
    summary: "抗初老精准护肤，配合抖音巨量千川投流，实现从深度评测到购物车下单闭环。",
    messages: [
      {
        id: "m2-1",
        sender: "user",
        text: "生成欧莱雅玻尿酸复颜系列的推广报告，这次在抖音做了比较重的千川投流，看看ROI表现。",
        timestamp: "昨天"
      },
      {
        id: "m2-2",
        sender: "ai",
        text: "已加载「巴黎欧莱雅」玻尿酸复颜系列抖音推广报告。\n\n由于本次加入了大规模的 **巨量千川信息流投流**，数据量呈爆发式增长：\n- **总曝光突破 24.5M**，其中千川付费流量占比达 45%。\n- **综合 ROI 达到 1.95**，虽然相比纯自然流有所稀释，但极大地扩大了品牌的“A3人群”（即对品牌有深层种草意向的人群）。\n- **受众年龄偏大一些**：主要集中在 **25-35岁都市白领**（占比达 65%以上），抗初老需求精准匹配。\n\n具体BI大盘指标和各达人明细已在右侧同步输出，欢迎审阅！",
        timestamp: "昨天"
      }
    ],
    reportData: {
      sentiment: {
        positive: 81,
        neutral: 14,
        negative: 5,
        keywords: ["吸收极快", "眼角细纹变淡", "大牌平替", "稍微黏稠", "保湿抗老", "微针质感"]
      },
      engagement: {
        totalViews: 24500000,
        totalLikes: 1150000,
        totalComments: 180000,
        totalShares: 220000,
        avgEngagementRate: 6.8,
        trendData: [
          { name: "Day 1", views: 1200000, engagement: 4.8 },
          { name: "Day 2", views: 2800000, engagement: 5.9 },
          { name: "Day 3", views: 5500000, engagement: 7.1 },
          { name: "Day 4", views: 6800000, engagement: 7.4 },
          { name: "Day 5", views: 4200000, engagement: 6.5 },
          { name: "Day 6", views: 2500000, engagement: 5.9 },
          { name: "Day 7", views: 1500000, engagement: 5.2 }
        ]
      },
      demographics: {
        gender: [
          { name: "女性 (Female)", value: 88, color: "#ec4899" },
          { name: "男性 (Male)", value: 12, color: "#3b82f6" }
        ],
        age: [
          { name: "18-24", value: 15 },
          { name: "25-30", value: 50 },
          { name: "31-35", value: 30 },
          { name: "36+", value: 5 }
        ],
        region: [
          { name: "上海", value: 26 },
          { name: "北京", value: 21 },
          { name: "江苏", value: 16 },
          { name: "广东", value: 15 },
          { name: "四川", value: 10 },
          { name: "其他", value: 12 }
        ]
      },
      mcnAnalysis: {
        mcnName: "微念传媒 (Weinian)",
        fulfillmentRate: 97,
        cpm: 52,
        cpe: 2.1,
        roi: 1.95,
        score: 91,
        strengths: ["千川投流精细化，精准覆盖高净值白领", "带货主播话术专业，承接率极佳"],
        weaknesses: ["佣金扣点偏高，压缩了部分利润空间", "对中尾部博主的二次内容分发不够频繁"]
      },
      kolPerformance: [
        {
          name: "Elena Zhou (科技美妆)",
          avatar: "",
          platform: "Douyin",
          followers: "3.2M",
          engagementRate: 7.8,
          cost: "¥120,000",
          salesConversion: "¥280,000",
          sentimentPositive: 84
        },
        {
          name: "Sunny Chen (轻奢生活)",
          avatar: "",
          platform: "Douyin",
          followers: "1.5M",
          engagementRate: 5.6,
          cost: "¥60,000",
          salesConversion: "¥115,000",
          sentimentPositive: 77
        }
      ],
      recommendations: [
        "1. **精细化千川人群包**：继续排除对价格高度敏感的低端学生人群，锁定一二线城市、25岁以上有明确“初抗老”消费力的大厂白领。",
        "2. **增加Bilibili科技向投放**：Elena Zhou的科学护肤成分党测评视频在B站往往能得到更长的生命周期，可与微念沟通做跨平台二次分发。",
        "3. **设立达人分级矩阵**：头部做信任背书，腰尾部红人做矩阵式使用感受分享，从而全网引爆欧莱雅的新系列口碑。"
      ]
    }
  },
  {
    id: "WO-1003",
    title: "Anker-数码极客海外测评",
    brand: "Anker (安克创新)",
    campaignName: "GaN闪充系列全球数码测评",
    status: "completed",
    platform: "Bilibili",
    category: "3C 数码",
    targetAudience: "18-30 岁数码爱好者",
    budgetMin: "80000",
    budgetMax: "200000",
    isStarred: false,
    createdAt: "2026-07-08T09:00:00+08:00",
    updatedAt: "2026-07-08T09:05:00+08:00",
    mcn: "科技矩阵 (TechMCN)",
    kols: ["TechVloger (数码大V)", "GizmoReview (极客玩家)"],
    summary: "硬核GaN技术测评，主打Bilibili/YouTube男性极客，深度剖析充电效率和温度控制。",
    messages: [
      {
        id: "m3-1",
        sender: "user",
        text: "安克闪充系列的报告呢？B站的极客受众好像对充电发热和功率很敏感，视频的弹幕舆情怎么样？",
        timestamp: "2天前"
      },
      {
        id: "m3-2",
        sender: "ai",
        text: "已成功提取「安克 (Anker) 闪充系列」B站测评活动的数据。\n\n针对极客受众对**参数**的苛刻要求，我们在分析中发现：\n- **弹幕热度高 (180K互动)**：B站的单次互动率（投币、收藏）高于常规平台。舆情表现出明显的极客倾向，最关注的是「温控」和「小体积高瓦数」。\n- **男性受众高达 85%**，主力人群是 **18-30岁数码发烧友**。\n- **ROI 达到 2.10**：数码品类客单价相对较高，通过高画质和深度实测，成功拉动了旗舰店大额券的使用。\n\nBI图表和详细舆情热词词云均已呈现在右侧，您可以查阅详细情况！",
        timestamp: "2天前"
      }
    ],
    reportData: {
      sentiment: {
        positive: 72,
        neutral: 24,
        negative: 4,
        keywords: ["温控绝了", "体积真的小", "真GaN技术", "充电脑没压力", "价格稍微贵", "线材硬了点"]
      },
      engagement: {
        totalViews: 6500000,
        totalLikes: 480000,
        totalComments: 95000,
        totalShares: 72000,
        avgEngagementRate: 8.5,
        trendData: [
          { name: "Day 1", views: 300000, engagement: 6.2 },
          { name: "Day 2", views: 950000, engagement: 7.9 },
          { name: "Day 3", views: 1800000, engagement: 8.8 },
          { name: "Day 4", views: 1400000, engagement: 8.9 },
          { name: "Day 5", views: 1100000, engagement: 8.4 },
          { name: "Day 6", views: 650000, engagement: 8.1 },
          { name: "Day 7", views: 300000, engagement: 7.2 }
        ]
      },
      demographics: {
        gender: [
          { name: "男性 (Male)", value: 85, color: "#3b82f6" },
          { name: "女性 (Female)", value: 15, color: "#ec4899" }
        ],
        age: [
          { name: "18-24", value: 45 },
          { name: "25-30", value: 38 },
          { name: "31-35", value: 12 },
          { name: "36+", value: 5 }
        ],
        region: [
          { name: "广东", value: 28 },
          { name: "北京", value: 18 },
          { name: "四川", value: 14 },
          { name: "湖北", value: 12 },
          { name: "江苏", value: 11 },
          { name: "其他", value: 17 }
        ]
      },
      mcnAnalysis: {
        mcnName: "科技矩阵 (TechMCN)",
        fulfillmentRate: 99,
        cpm: 65,
        cpe: 1.5,
        roi: 2.1,
        score: 93,
        strengths: ["达人硬核专业，能够进行复杂的拆解和负载波形图展示", "极客粉丝粘性极强，购买意愿转换极高"],
        weaknesses: ["制作周期非常长，无法适应短频快宣发需求", "视频对小白用户有一定门槛，不利于全品类出圈"]
      },
      kolPerformance: [
        {
          name: "TechVloger (数码大V)",
          avatar: "",
          platform: "Bilibili",
          followers: "1.8M",
          engagementRate: 9.1,
          cost: "¥90,000",
          salesConversion: "¥210,000",
          sentimentPositive: 75
        },
        {
          name: "GizmoReview (极客玩家)",
          avatar: "",
          platform: "Bilibili",
          followers: "650K",
          engagementRate: 7.4,
          cost: "¥35,000",
          salesConversion: "¥74,000",
          sentimentPositive: 68
        }
      ],
      recommendations: [
        "1. **建立极客专属优惠代码**：数码评测视频发布后的24小时是流量黄金期，达人置顶动态专属优惠券能极大地追踪直接销售来源。",
        "2. **配合高能剪辑分发**：将B站深度测评中高能拆解和烧机测试的高光片段，混剪成15秒高节奏短视频分发到抖音，降低极客门槛吸引泛大众。",
        "3. **强调安全性背书**：在舆情分析中，部分用户关注「是否伤手机电池」。在未来宣发脚本中增加对『智能降流、低温充电不伤机』的核心篇幅阐述。"
      ]
    }
  }
];
