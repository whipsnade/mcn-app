import { Message, ReportData } from './types';

// Payload sent to the analyze endpoint (mirrors what the Express backend expects).
export interface AnalyzePayload {
  messages: Message[];
  brand: string;
  campaignName: string;
  platform: string;
  mcn: string;
  kols: string[];
  currentReportData?: ReportData | null;
}

export interface AnalyzeResult {
  reply: string;
  reportData: ReportData;
  isMock: boolean;
}

// Offline mock report generator — mirrors the fallback logic in server.ts so the
// app stays fully usable on static hosting (e.g. GitHub Pages) with no backend.
export function getMockReportData(
  brand: string,
  campaign: string,
  platform: string,
  mcn: string,
  kols: string[]
): ReportData {
  const cleanKols = kols.length > 0 ? kols : ['Alice Freeman', 'Diana Prince', 'Jerry Wang'];
  const cleanMcn = mcn || '星耀互娱 (StarMCN)';

  return {
    sentiment: {
      positive: 76,
      neutral: 17,
      negative: 7,
      keywords: ['效果惊艳', '成分温和', '回购回购', '略微贵了', '种草了', 'MCN配合度高'],
    },
    engagement: {
      totalViews: 8500000,
      totalLikes: 420000,
      totalComments: 68000,
      totalShares: 35000,
      avgEngagementRate: 6.1,
      trendData: [
        { name: 'Day 1', views: 500000, engagement: 4.2 },
        { name: 'Day 2', views: 1200000, engagement: 5.5 },
        { name: 'Day 3', views: 2100000, engagement: 6.3 },
        { name: 'Day 4', views: 1800000, engagement: 6.8 },
        { name: 'Day 5', views: 1500000, engagement: 6.2 },
        { name: 'Day 6', views: 900000, engagement: 5.8 },
        { name: 'Day 7', views: 500000, engagement: 5.1 },
      ],
    },
    demographics: {
      gender: [
        { name: '女性 (Female)', value: 78, color: '#ec4899' },
        { name: '男性 (Male)', value: 22, color: '#3b82f6' },
      ],
      age: [
        { name: '18-24', value: 42 },
        { name: '25-30', value: 38 },
        { name: '31-35', value: 14 },
        { name: '36+', value: 6 },
      ],
      region: [
        { name: '广东 (Guangdong)', value: 24 },
        { name: '上海 (Shanghai)', value: 18 },
        { name: '北京 (Beijing)', value: 15 },
        { name: '浙江 (Zhejiang)', value: 13 },
        { name: '四川 (Sichuan)', value: 11 },
        { name: '其他 (Others)', value: 19 },
      ],
    },
    mcnAnalysis: {
      mcnName: cleanMcn,
      fulfillmentRate: 96,
      cpm: 45,
      cpe: 2.8,
      roi: 2.4,
      score: 88,
      strengths: ['KOL矩阵执行力强', '视频审核及脚本修改极速', '核心腰部达人爆文率高'],
      weaknesses: ['头部网红排期较紧', '溢价稍微偏高', '直播带货转化有待优化'],
    },
    kolPerformance: cleanKols.map((kol, index) => {
      const platforms: KOLPlatform[] = ['Xiaohongshu', 'Douyin', 'Bilibili', 'Weibo'];
      const pf = (platform as KOLPlatform) || platforms[index % platforms.length];
      const followerArr = ['1.2M', '2.5M', '850K', '3.1M'];
      const costArr = ['¥45,000', '¥85,000', '¥32,000', '¥110,000'];
      const rates = [5.8, 6.5, 8.2, 4.1];
      const sPos = [78, 82, 72, 85];

      return {
        name: kol,
        avatar: '',
        platform: pf,
        followers: followerArr[index % followerArr.length],
        engagementRate: rates[index % rates.length],
        cost: costArr[index % costArr.length],
        salesConversion: `¥${((index + 1) * 75000).toLocaleString()}`,
        sentimentPositive: sPos[index % sPos.length],
      };
    }),
    recommendations: [
      `1. 加大在 ${platform || '小红书'} 平台中长尾达人的合作比例，以提升品牌日常真实检索率。`,
      `2. 针对 MCN ${cleanMcn}，在接下来的大促节点中争取更低的保量包销折扣，降低 CPE 获客成本。`,
      '3. 核心达人视频中的痛点场景前置，进一步优化黄金3秒完播率，从而进入更高一级自然流量池。',
    ],
  };
}

type KOLPlatform = ReportData['kolPerformance'][number]['platform'];

// Produces a mock analyst reply + report for a given payload, keyword-aware like the backend.
export function analyzeMock(payload: AnalyzePayload): AnalyzeResult {
  const { messages, brand, campaignName, platform, mcn, kols } = payload;
  const latestUserMsg = messages[messages.length - 1]?.text || '';
  const mockReport = getMockReportData(brand, campaignName, platform, mcn, kols);

  let reply = `【离线分析模式】您好！由于当前为静态托管环境（未配置后端 AI 服务），我已为您自动生成了针对「${brand || '某品牌'}」品牌「${campaignName || '本次营销'}」活动的标准化分析模型。

在当前的数据模拟中：
1. 整体受众以 **18-30岁女性** 占比最高（约${mockReport.demographics.gender[0].value}%），符合美妆、时尚类轻奢品牌的基本画像。
2. 社交媒体舆情正向率达 **${mockReport.sentiment.positive}%**，高频词包括「${mockReport.sentiment.keywords[0]}」和「${mockReport.sentiment.keywords[1]}」。
3. 合作的 MCN **${mockReport.mcnAnalysis.mcnName}** 综合评分为 **${mockReport.mcnAnalysis.score}分**，ROI 约为 **${mockReport.mcnAnalysis.roi}**。

*部署带后端服务（配置 GEMINI_API_KEY）后即可解锁完全动态的 AI 多维度洞察与全量定制化报表生成！*`;

  if (latestUserMsg.includes('情感') || latestUserMsg.includes('舆情') || latestUserMsg.includes('sentiment')) {
    reply = `【离线舆情分析】针对您的提问，我们对本次活动的达人评论区进行了情感抽样（样本量 N=5,000）：
- **正面舆情 (${mockReport.sentiment.positive}%)**：用户对产品功能和外观赞不绝口，爆款词汇为「${mockReport.sentiment.keywords.slice(0, 3).join('、')}」。
- **中立舆情 (${mockReport.sentiment.neutral}%)**：多为询问购买渠道、价格、优惠券，以及询问是否有其他色号/款式。
- **负面舆情 (${mockReport.sentiment.negative}%)**：主要集中在「${mockReport.sentiment.keywords[3]}」（约占负面的 60%），建议品牌在后续内容中强调性价比或推出中样体验装。`;
  } else if (
    latestUserMsg.includes('ROI') ||
    latestUserMsg.includes('转化') ||
    latestUserMsg.includes('MCN') ||
    latestUserMsg.includes('费用')
  ) {
    reply = `【离线MCN分析】MCN 机构 **${mockReport.mcnAnalysis.mcnName}** 表现如下：
- **ROI 评估**：本次推广实际销售转化比为 **${mockReport.mcnAnalysis.roi}**，在行业大盘处于中上等水平。
- **核心优势**：${mockReport.mcnAnalysis.strengths.join('、')}。
- **主要瓶颈**：${mockReport.mcnAnalysis.weaknesses.join('、')}。
建议后续可以针对转化率高的头部达人追加投流（如 Douyin 随心推 / Red 薯条），实现二次破圈。`;
  } else if (latestUserMsg.trim().length > 0) {
    reply = `【离线分析师解答】收到关于「${latestUserMsg}」的反馈。
在 KOL 矩阵中，**${mockReport.kolPerformance[0]?.name || '核心达人'}** 以较强的带货爆发力成为本次亮点，其粉丝正向情感率达 **${mockReport.kolPerformance[0]?.sentimentPositive || 80}%**。为了针对性优化，已在右侧「AI 推荐」中更新了相应的定制化策略，请您参考。`;
  }

  return { reply, reportData: mockReport, isMock: true };
}
