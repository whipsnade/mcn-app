import express from "express";
import path from "path";
import { createServer as createViteServer } from "vite";
import { GoogleGenAI, Type } from "@google/genai";
import dotenv from "dotenv";

dotenv.config();

const app = express();
app.use(express.json());

const PORT = 3000;

// Lazy initialization of Gemini AI to prevent crash if key is missing
function getGeminiClient() {
  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) {
    return null;
  }
  return new GoogleGenAI({
    apiKey: apiKey,
    httpOptions: {
      headers: {
        'User-Agent': 'aistudio-build',
      }
    }
  });
}

// Fallback Mock Data Generator for when Gemini API Key is missing or fails
function getMockReportData(brand: string, campaign: string, platform: string, mcn: string, kols: string[]): any {
  const cleanKols = kols.length > 0 ? kols : ["Alice Freeman", "Diana Prince", "Jerry Wang"];
  const cleanMcn = mcn || "星耀互娱 (StarMCN)";
  
  return {
    sentiment: {
      positive: 76,
      neutral: 17,
      negative: 7,
      keywords: ["效果惊艳", "成分温和", "回购回购", "略微贵了", "种草了", "配合度高"]
    },
    engagement: {
      totalViews: 8500000,
      totalLikes: 420000,
      totalComments: 68000,
      totalShares: 35000,
      avgEngagementRate: 6.1,
      trendData: [
        { name: "Day 1", views: 500000, engagement: 4.2 },
        { name: "Day 2", views: 1200000, engagement: 5.5 },
        { name: "Day 3", views: 2100000, engagement: 6.3 },
        { name: "Day 4", views: 1800000, engagement: 6.8 },
        { name: "Day 5", views: 1500000, engagement: 6.2 },
        { name: "Day 6", views: 900000, engagement: 5.8 },
        { name: "Day 7", views: 500000, engagement: 5.1 }
      ]
    },
    demographics: {
      gender: [
        { name: "女性 (Female)", value: 78, color: "#ec4899" },
        { name: "男性 (Male)", value: 22, color: "#3b82f6" }
      ],
      age: [
        { name: "18-24", value: 42 },
        { name: "25-30", value: 38 },
        { name: "31-35", value: 14 },
        { name: "36+", value: 6 }
      ],
      region: [
        { name: "广东 (Guangdong)", value: 24 },
        { name: "上海 (Shanghai)", value: 18 },
        { name: "北京 (Beijing)", value: 15 },
        { name: "浙江 (Zhejiang)", value: 13 },
        { name: "四川 (Sichuan)", value: 11 },
        { name: "其他 (Others)", value: 19 }
      ]
    },
    mcnAnalysis: {
      mcnName: cleanMcn,
      fulfillmentRate: 96,
      cpm: 45,
      cpe: 2.8,
      roi: 2.4,
      score: 88,
      strengths: ["KOL矩阵执行力强", "视频审核及脚本修改极速", "核心达人爆文率高"],
      weaknesses: ["头部排期较紧", "溢价稍微偏高", "直播带货转化有待优化"]
    },
    kolPerformance: cleanKols.map((kol, index) => {
      const platforms: any[] = ["Xiaohongshu", "Douyin", "Bilibili", "Weibo"];
      const pf = platform || platforms[index % platforms.length];
      const followerArr = ["1.2M", "2.5M", "850K", "3.1M"];
      const costArr = ["¥45,000", "¥85,000", "¥32,000", "¥110,000"];
      const rates = [5.8, 6.5, 8.2, 4.1];
      const sPos = [78, 82, 72, 85];
      
      return {
        name: kol,
        avatar: "",
        platform: pf,
        followers: followerArr[index % followerArr.length],
        engagementRate: rates[index % rates.length],
        cost: costArr[index % costArr.length],
        salesConversion: `¥${((index + 1) * 75000).toLocaleString()}`,
        sentimentPositive: sPos[index % sPos.length]
      };
    }),
    recommendations: [
      `1. 加大在 ${platform || "小红书"} 平台中长尾达人的合作比例，以提升品牌日常真实检索率。`,
      `2. 结合达人的历史多维度互动与内容质量，进行更精细化的预算倾斜。`,
      "3. 核心达人视频中的话题标签及痛点前置，进一步优化黄金3秒留存率，从而进入更高一级自然流量池。"
    ]
  };
}

// API endpoint to analyze and chat
app.post("/api/analyze", async (req, res) => {
  const { messages, brand, campaignName, platform, mcn, kols, currentReportData } = req.body;
  
  const ai = getGeminiClient();
  
  if (!ai) {
    // Return simulated intelligent analysis when API key is missing
    console.warn("GEMINI_API_KEY environment variable is not defined. Using offline mock analyst mode.");
    const latestUserMsg = messages[messages.length - 1]?.text || "";
    const mockReport = getMockReportData(brand, campaignName, platform, mcn, kols);
    
    // Add custom response based on keywords
    let reply = `【离线分析模式】您好！由于尚未配置 GEMINI_API_KEY 密钥，我已为您自动生成了针对「${brand || "某品牌"}」品牌「${campaignName || "本次营销"}」活动的标准化声量与KOL分析模型。

在当前的数据模拟中：
1. 整体受众以 **18-30岁女性** 占比最高（约${mockReport.demographics.gender[0].value}%），符合美妆、时尚类轻奢品牌的基本画像，支持 \`social_statistic_user_profile\` 提取。
2. 社交媒体舆情正向率达 **${mockReport.sentiment.positive}%**，高频词包括「${mockReport.sentiment.keywords[0]}」和「${mockReport.sentiment.keywords[1]}」，支持 \`query_analysis_data\` 提取。
3. 关联活动总曝光达 **${(mockReport.engagement.totalViews / 1000000).toFixed(1)}M次**，符合 \`social_statistic_overview\` 概览指标。

*您可以配置系统的 GEMINI_API_KEY 以解锁完全动态的 AI 多维度洞察与全量定制化的报表生成！*`;

    if (latestUserMsg.includes("情感") || latestUserMsg.includes("舆情") || latestUserMsg.includes("sentiment")) {
      reply = `【离线舆情分析】针对您的提问，我们通过 \`query_analysis_data\` 对本次活动的达人评论区进行了情感抽样（样本量 N=5,000）：
- **正面舆情 (${mockReport.sentiment.positive}%)**：用户对产品功能与外观赞不绝口，爆款词汇为「${mockReport.sentiment.keywords.slice(0, 3).join("、")}」。
- **中立舆情 (${mockReport.sentiment.neutral}%)**：多为询问购买渠道、价格、优惠券，以及询问是否有其他色号/款式。
- **负面舆情 (${mockReport.sentiment.negative}%)**：主要集中在「${mockReport.sentiment.keywords[3]}」（约占负面的 60%），建议品牌在后续内容中强调产品特点或温和配方。`;
    } else if (latestUserMsg.includes("KOL") || latestUserMsg.includes("达人") || latestUserMsg.includes("达人数据") || latestUserMsg.includes("红人")) {
      reply = `【离线KOL看板分析】结合 \`social_statistic_hot_user\` 和 \`query_user_info\` 得到的达人表现如下：
- **核心声量贡献**：本次红人矩阵整体表现稳健。头部达人以极高互动率撬动圈层，其单人声量贡献比高达 **${((mockReport.kolPerformance[0]?.engagementRate * 5.4) + 12).toFixed(1)}%**。
- **互动粘性**：平均互动率达 **${mockReport.engagement.avgEngagementRate}%**，说明粉丝粘性强，正向态度好。
您可以点击右侧的「KOL看板」查看每位达人的具体粉丝量、互动率、成本及声量贡献占比。`;
    } else if (latestUserMsg.trim().length > 0) {
      reply = `【离线分析师解答】收到关于「${latestUserMsg}」的提问。
基于 \`social_statistic_overview\` 和 \`query_raw_posts\` 聚合：
在 KOL 矩阵中，**${mockReport.kolPerformance[0]?.name || "核心达人"}** 以较强的社交爆发力成为本次亮点，其粉丝正向情感率达 **${mockReport.kolPerformance[0]?.sentimentPositive || 80}%**。为了更深入地分析其传播路径与全网品牌声量趋势，请查看右侧的「数据看板」中相关的多维画像和时序波峰。`;
    }

    return res.json({
      reply,
      reportData: mockReport,
      isMock: true
    });
  }

  try {
    const latestUserMsg = messages[messages.length - 1]?.text || "请根据提供的基本参数，分析此红人KOL和活动声量效果。";
    
    // Construct system prompt and instructions
    const systemInstruction = `You are a Senior Influencer Marketing & Social Media Volume Analyst (高级网红KOL与社交媒体声量分析数据专家).
Your objective is to help brand marketing managers analyze campaign performance, brand volume, public sentiment, and review individual KOL performance. Currently, the data MCP only provides functions such as social_statistic_overview, query_analysis_data, social_statistic_trend, social_statistic_user_profile, social_statistic_hot_user, and query_user_info.

Do NOT generate or discuss MCN ratings, ROI, or AI strategic recommendations. Focus entirely on compiling the following:
1. Data Dashboard (数据看板):
   - Brand Sentiment analysis (positive, neutral, negative and top keywords based on query_analysis_data).
   - Brand Volume & Engagement metrics (total views, likes, comments, shares, average rate, based on social_statistic_overview).
   - Volume Trend (social_statistic_trend chart).
   - Target Demographics (gender ratio, age groups, and top provinces based on social_statistic_user_profile).
2. KOL Dashboard (KOL看板):
   - Individual KOL/Creator detailed metrics list (followers, platform, engagement rate, cost, sentiment, and calculated volume contribution based on social_statistic_hot_user and query_user_info).

You must respond with a JSON object containing:
- "reply": A highly professional, conversational, data-backed analysis response in Chinese. Discuss specific numbers, praise outstanding creators, point out bottlenecks based on volume or engagement, and reply to the user's latest query naturally. Make it detailed and structured.
- "reportData": The fully-formed BI analytics structured report data following the precise type definitions.

If the user requests to change, simulate, or adjust any metric in the conversation (e.g., "Change Alice's positive feedback to 85%?"), you MUST adjust the returned 'reportData' to reflect these changes exactly.

The 'reportData' schema must strictly match:
{
  sentiment: { positive: number, neutral: number, negative: number, keywords: string[] },
  engagement: {
    totalViews: number,
    totalLikes: number,
    totalComments: number,
    totalShares: number,
    avgEngagementRate: number, // e.g. 5.4 (representing 5.4%)
    trendData: Array<{ name: string, views: number, engagement: number }> // 7 data points
  },
  demographics: {
    gender: Array<{ name: string, value: number, color: string }>, // two items, sum = 100, female first
    age: Array<{ name: string, value: number }>, // items: e.g. "18-24", "25-30", "31-35", "36+"
    region: Array<{ name: string, value: number }> // top 5-6 provinces
  },
  mcnAnalysis: {
    mcnName: string,
    fulfillmentRate: number, // scale 0-100
    cpm: number, // cost per mille in CNY
    cpe: number, // cost per engagement in CNY
    roi: number, // ratio e.g. 2.4
    score: number, // 0-100
    strengths: string[],
    weaknesses: string[]
  },
  kolPerformance: Array<{
    name: string,
    avatar: string,
    platform: string, // e.g., "Xiaohongshu", "Douyin", "Bilibili", "Weibo"
    followers: string, // e.g., "1.2M"
    engagementRate: number, // e.g., 6.2
    cost: string, // e.g., "¥45,000"
    salesConversion: string, // e.g., "¥120,000"
    sentimentPositive: number // e.g. 84
  }>,
  recommendations: string[] // 3-4 professional recommendations in Chinese
}`;

    const prompt = `Campaign Context:
- Brand Name: ${brand || "未设定品牌"}
- Campaign Name: ${campaignName || "未设定活动"}
- Platform Target: ${platform || "小红书/抖音"}
- Cooperative MCN Agency: ${mcn || "默认优质MCN"}
- KOLs list: ${kols?.length > 0 ? kols.join(", ") : "未指定达人列表"}

Current BI Report State (Use this as your baseline and update it based on user feedback!):
${currentReportData ? JSON.stringify(currentReportData) : "No existing report. Please generate a realistic, standard-compliant set of metrics."}

Full Conversation History:
${messages.map((m: any) => `${m.sender === 'user' ? 'Client' : 'Analyst'}: ${m.text}`).join('\n')}

Latest User Message to respond to: "${latestUserMsg}"

Please analyze and generate the perfect Chinese reply along with the complete updated 'reportData' matching the exact JSON schema.`;

    const response = await ai.models.generateContent({
      model: "gemini-3.5-flash",
      contents: prompt,
      config: {
        systemInstruction,
        responseMimeType: "application/json",
        responseSchema: {
          type: Type.OBJECT,
          required: ["reply", "reportData"],
          properties: {
            reply: {
              type: Type.STRING,
              description: "Detailed analysis chat reply in Chinese. Discuss the metrics, provide concrete strategic comments, and answer any specific questions."
            },
            reportData: {
              type: Type.OBJECT,
              description: "Complete structured BI dashboard parameters matching the client's campaign scenario.",
              properties: {
                sentiment: {
                  type: Type.OBJECT,
                  required: ["positive", "neutral", "negative", "keywords"],
                  properties: {
                    positive: { type: Type.INTEGER },
                    neutral: { type: Type.INTEGER },
                    negative: { type: Type.INTEGER },
                    keywords: { type: Type.ARRAY, items: { type: Type.STRING } }
                  }
                },
                engagement: {
                  type: Type.OBJECT,
                  required: ["totalViews", "totalLikes", "totalComments", "totalShares", "avgEngagementRate", "trendData"],
                  properties: {
                    totalViews: { type: Type.INTEGER },
                    totalLikes: { type: Type.INTEGER },
                    totalComments: { type: Type.INTEGER },
                    totalShares: { type: Type.INTEGER },
                    avgEngagementRate: { type: Type.NUMBER },
                    trendData: {
                      type: Type.ARRAY,
                      items: {
                        type: Type.OBJECT,
                        required: ["name", "views", "engagement"],
                        properties: {
                          name: { type: Type.STRING },
                          views: { type: Type.INTEGER },
                          engagement: { type: Type.NUMBER }
                        }
                      }
                    }
                  }
                },
                demographics: {
                  type: Type.OBJECT,
                  required: ["gender", "age", "region"],
                  properties: {
                    gender: {
                      type: Type.ARRAY,
                      items: {
                        type: Type.OBJECT,
                        required: ["name", "value", "color"],
                        properties: {
                          name: { type: Type.STRING },
                          value: { type: Type.NUMBER },
                          color: { type: Type.STRING }
                        }
                      }
                    },
                    age: {
                      type: Type.ARRAY,
                      items: {
                        type: Type.OBJECT,
                        required: ["name", "value"],
                        properties: {
                          name: { type: Type.STRING },
                          value: { type: Type.NUMBER }
                        }
                      }
                    },
                    region: {
                      type: Type.ARRAY,
                      items: {
                        type: Type.OBJECT,
                        required: ["name", "value"],
                        properties: {
                          name: { type: Type.STRING },
                          value: { type: Type.NUMBER }
                        }
                      }
                    }
                  }
                },
                mcnAnalysis: {
                  type: Type.OBJECT,
                  required: ["mcnName", "fulfillmentRate", "cpm", "cpe", "roi", "score", "strengths", "weaknesses"],
                  properties: {
                    mcnName: { type: Type.STRING },
                    fulfillmentRate: { type: Type.INTEGER },
                    cpm: { type: Type.INTEGER },
                    cpe: { type: Type.NUMBER },
                    roi: { type: Type.NUMBER },
                    score: { type: Type.INTEGER },
                    strengths: { type: Type.ARRAY, items: { type: Type.STRING } },
                    weaknesses: { type: Type.ARRAY, items: { type: Type.STRING } }
                  }
                },
                kolPerformance: {
                  type: Type.ARRAY,
                  items: {
                    type: Type.OBJECT,
                    required: ["name", "platform", "followers", "engagementRate", "cost", "sentimentPositive"],
                    properties: {
                      name: { type: Type.STRING },
                      avatar: { type: Type.STRING },
                      platform: { type: Type.STRING },
                      followers: { type: Type.STRING },
                      engagementRate: { type: Type.NUMBER },
                      cost: { type: Type.STRING },
                      salesConversion: { type: Type.STRING },
                      sentimentPositive: { type: Type.INTEGER }
                    }
                  }
                },
                recommendations: {
                  type: Type.ARRAY,
                  items: { type: Type.STRING }
                }
              }
            }
          }
        }
      }
    });

    const parsedResponse = JSON.parse(response.text || "{}");
    res.json(parsedResponse);
  } catch (error: any) {
    console.error("Gemini API Error during analysis generation:", error);
    res.status(500).json({
      error: error.message || "An error occurred during Gemini AI processing.",
      details: "Falling back to simulated static reporting."
    });
  }
});

// Setup Vite & static assets
async function startServer() {
  if (process.env.NODE_ENV !== "production") {
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: "spa",
    });
    app.use(vite.middlewares);
    console.log("Vite development server loaded as middleware");
  } else {
    const distPath = path.join(process.cwd(), "dist");
    app.use(express.static(distPath));
    app.get("*", (req, res) => {
      res.sendFile(path.join(distPath, "index.html"));
    });
    console.log(`Serving static files in production from: ${distPath}`);
  }

  app.listen(PORT, "0.0.0.0", () => {
    console.log(`KOL MCN BI Server running on http://0.0.0.0:${PORT}`);
  });
}

startServer();
