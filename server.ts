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
      keywords: ["效果惊艳", "成分温和", "回购回购", "略微贵了", "种草了", "MCN配合度高"]
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
      strengths: ["KOL矩阵执行力强", "视频审核及脚本修改极速", "核心腰部达人爆文率高"],
      weaknesses: ["头部网红排期较紧", "溢价稍微偏高", "直播带货转化有待优化"]
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
      `2. 针对 MCN ${cleanMcn}，在接下来的大促节点中争取更低的保量包销折扣，降低 CPE 获客成本。`,
      "3. 核心达人视频中的痛点场景前置，进一步优化黄金3秒完播率，从而进入更高一级自然流量池。"
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
    let reply = `【离线分析模式】您好！由于尚未配置 GEMINI_API_KEY 密钥，我已为您自动生成了针对「${brand || "某品牌"}」品牌「${campaignName || "本次营销"}」活动的标准化分析模型。

在当前的数据模拟中：
1. 整体受众以 **18-30岁女性** 占比最高（约${mockReport.demographics.gender[0].value}%），符合美妆、时尚类轻奢品牌的基本画像。
2. 社交媒体舆情正向率达 **${mockReport.sentiment.positive}%**，高频词包括「${mockReport.sentiment.keywords[0]}」和「${mockReport.sentiment.keywords[1]}」。
3. 合作的 MCN **${mockReport.mcnAnalysis.mcnName}** 综合评分为 **${mockReport.mcnAnalysis.score}分**，ROI 约为 **${mockReport.mcnAnalysis.roi}**。

*您可以配置系统的 GEMINI_API_KEY 以解锁完全动态的 AI 多维度洞察与全量定制化的报表生成！*`;

    if (latestUserMsg.includes("情感") || latestUserMsg.includes("舆情") || latestUserMsg.includes("sentiment")) {
      reply = `【离线舆情分析】针对您的提问，我们对本次活动的达人评论区进行了情感抽样（样本量 N=5,000）：
- **正面舆情 (${mockReport.sentiment.positive}%)**：用户对产品功能和外观赞不绝口，爆款词汇为「${mockReport.sentiment.keywords.slice(0, 3).join("、")}」。
- **中立舆情 (${mockReport.sentiment.neutral}%)**：多为询问购买渠道、价格、优惠券，以及询问是否有其他色号/款式。
- **负面舆情 (${mockReport.sentiment.negative}%)**：主要集中在「${mockReport.sentiment.keywords[3]}」（约占负面的 60%），建议品牌在后续内容中强调性价比或推出中样体验装。`;
    } else if (latestUserMsg.includes("ROI") || latestUserMsg.includes("转化") || latestUserMsg.includes("MCN") || latestUserMsg.includes("费用")) {
      reply = `【离线MCN分析】MCN 机构 **${mockReport.mcnAnalysis.mcnName}** 表现如下：
- **ROI 评估**：本次推广实际销售转化比为 **${mockReport.mcnAnalysis.roi}**，在行业大盘处于中上等水平。
- **核心优势**：${mockReport.mcnAnalysis.strengths.join("、")}。
- **主要瓶颈**：${mockReport.mcnAnalysis.weaknesses.join("、")}。
建议后续可以针对转化率高的头部达人追加投流（如 Douyin 随心推 / Red 薯条），实现二次破圈。`;
    } else if (latestUserMsg.trim().length > 0) {
      reply = `【离线分析师解答】收到关于「${latestUserMsg}」的反馈。
在 KOL 矩阵中，**${mockReport.kolPerformance[0]?.name || "核心达人"}** 以较强的带货爆发力成为本次亮点，其粉丝正向情感率达 **${mockReport.kolPerformance[0]?.sentimentPositive || 80}%**。为了针对性优化，已在右侧「AI 推荐」中更新了相应的定制化策略，请您参考。`;
    }

    return res.json({
      reply,
      reportData: mockReport,
      isMock: true
    });
  }

  try {
    const latestUserMsg = messages[messages.length - 1]?.text || "请根据提供的基本参数，分析此网红KOL和MCN活动的效果。";
    
    // Construct system prompt and instructions
    const systemInstruction = `You are a Senior Influencer Marketing & MCN ROI Analyst (高级网红KOL与MCN营销效果数据分析专家).
Your objective is to help brand marketing managers analyze campaign performance, estimate ROI, evaluate the cooperative MCN agency, review individual KOL performance, and generate a dynamic BI report containing:
1. Sentiment analysis (positive, neutral, negative and top keywords).
2. Engagement metrics (total views, likes, comments, shares, average rate, and trend line).
3. Demographics (gender ratio, age groups, and top provinces).
4. MCN evaluation (agency name, execution score, fulfillment rate, CPM, CPE, ROI, strengths, weaknesses).
5. KOL detailed metrics list.
6. Custom recommendations for future tactics.

You must respond with a JSON object containing:
- "reply": A highly professional, conversational, data-backed analysis response in Chinese. Discuss specific numbers, praise outstanding creators, point out bottlenecks, and reply to the user's latest query naturally. Make it detailed and structured.
- "reportData": The fully-formed BI analytics structured report data following the precise type definitions.

If the user requests to change, simulate, or adjust any metric in the conversation (e.g., "Change the ROI to 3.2", "Set MCN score to 95", "Add Alice's negative feedback about pricing", "What if positive sentiment was 85%?"), you MUST adjust the returned 'reportData' to reflect these changes exactly.

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
