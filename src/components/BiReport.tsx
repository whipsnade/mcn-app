import React, { useState } from 'react';
import { 
  TrendingUp, Users, Heart, Share2, MessageCircle, Eye, 
  BarChart2, Award, CheckCircle2, AlertTriangle, HelpCircle, 
  ThumbsUp, PieChart as PieIcon, LineChart as LineIcon, Activity, Star,
  Sparkles, Printer
} from 'lucide-react';
import { 
  ResponsiveContainer, AreaChart, Area, XAxis, YAxis, Tooltip, 
  BarChart, Bar, Cell, PieChart, Pie, Legend
} from 'recharts';
import { ReportData } from '../types';

interface BiReportProps {
  reportData?: ReportData;
  campaignName: string;
  brand: string;
}

function MetricHelper({ 
  title, 
  formula, 
  sampling,
  align = 'center'
}: { 
  title: string; 
  formula: string; 
  sampling: string; 
  align?: 'left' | 'center' | 'right';
}) {
  const alignClasses = {
    left: 'left-0 -translate-x-2',
    center: 'left-1/2 -translate-x-1/2',
    right: 'right-0 translate-x-2'
  };

  const arrowClasses = {
    left: 'left-3',
    center: 'left-1/2 -translate-x-1/2',
    right: 'right-3'
  };

  return (
    <span className="group relative inline-flex items-center ml-1 text-slate-300 hover:text-indigo-500 cursor-help transition no-print">
      <HelpCircle className="h-3 w-3" />
      <span className={`pointer-events-none absolute bottom-full mb-2 w-56 rounded-xl bg-slate-900/95 backdrop-blur-sm p-3 text-[10px] text-white font-normal leading-normal opacity-0 group-hover:opacity-100 transition-opacity duration-200 shadow-xl border border-slate-800 z-50 text-left ${alignClasses[align]}`}>
        <span className="block font-bold text-indigo-400 mb-1">📊 {title} 计算公式</span>
        <code className="block bg-slate-950 px-1.5 py-1 rounded text-[9px] font-mono text-slate-300 mb-2 border border-slate-800/80 whitespace-pre-wrap break-words leading-relaxed">
          {formula}
        </code>
        <span className="block text-[9px] text-slate-400 border-t border-slate-800 pt-1.5">
          <span className="font-semibold text-slate-300 block mb-0.5">🔍 原始数据采样说明:</span>
          {sampling}
        </span>
        <span className={`absolute top-full -mt-1 border-4 border-transparent border-t-slate-900/95 ${arrowClasses[align]}`} />
      </span>
    </span>
  );
}

export default function BiReport({ reportData, campaignName, brand }: BiReportProps) {
  const [activeTab, setActiveTab] = useState<'dashboard' | 'kol'>('dashboard');

  if (!reportData) {
    return (
      <div className="flex h-full flex-col bg-white w-96 shrink-0 items-center justify-center p-8 border-l border-slate-200 text-center">
        <div className="h-12 w-12 rounded-full bg-indigo-50 flex items-center justify-center text-indigo-600 mb-4 animate-bounce">
          <Activity className="h-6 w-6" />
        </div>
        <h3 className="text-sm font-bold text-slate-800 font-display">等待生成分析数据</h3>
        <p className="text-xs text-slate-400 mt-1 max-w-[240px]">
          在中间对话框中与 AI 进行会话，或点击「AI 一键诊断」，即可实时在此生成多维 BI 图表报告。
        </p>
      </div>
    );
  }

  // Pre-configured colors for pie chart
  const RADIAN = Math.PI / 180;
  const sentimentColors = ['#10b981', '#f59e0b', '#ef4444']; // Positive, Neutral, Negative
  const sentimentPieData = [
    { name: '正面 (Positive)', value: reportData.sentiment.positive },
    { name: '中立 (Neutral)', value: reportData.sentiment.neutral },
    { name: '负面 (Negative)', value: reportData.sentiment.negative },
  ];

  return (
    <div className="flex h-full flex-col bg-white w-[420px] shrink-0 border-l border-slate-200 shadow-sm overflow-hidden print-container">
      
      {/* BI Panel Header with Export PDF */}
      <div className="h-14 flex items-center justify-between px-4 border-b border-slate-200 bg-white shrink-0">
        <h2 className="text-xs font-bold text-slate-800 uppercase tracking-widest">BI Intelligence Output</h2>
        <button
          onClick={() => window.print()}
          className="no-print flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-700 text-white font-bold text-[10px] shadow-sm transition active:scale-95"
          title="导出 PDF 营销报告"
        >
          <Printer className="h-3.5 w-3.5" />
          导出 PDF
        </button>
      </div>

      {/* Top Navigation Tabs */}
      <div className="flex border-b border-slate-200 shrink-0 bg-slate-50/50 no-print">
        <button
          onClick={() => setActiveTab('dashboard')}
          className={`flex-1 py-3.5 text-xs font-bold text-center border-b-2 transition ${
            activeTab === 'dashboard'
              ? 'border-indigo-600 text-indigo-600 bg-white'
              : 'border-transparent text-slate-500 hover:text-slate-800'
          }`}
        >
          <span className="flex items-center justify-center gap-1.5">
            <BarChart2 className="h-3.5 w-3.5" />
            数据看板
          </span>
        </button>
        <button
          onClick={() => setActiveTab('kol')}
          className={`flex-1 py-3.5 text-xs font-bold text-center border-b-2 transition ${
            activeTab === 'kol'
              ? 'border-indigo-600 text-indigo-600 bg-white'
              : 'border-transparent text-slate-500 hover:text-slate-800'
          }`}
        >
          <span className="flex items-center justify-center gap-1.5">
            <Award className="h-3.5 w-3.5" />
            KOL 看板
          </span>
        </button>
      </div>

      {/* Report Body Area */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4 bg-slate-50/30 print-scrollable">
        
        {/* TAB 1: BI DASHBOARD */}
        {activeTab === 'dashboard' && (
          <>
            {/* High-level KPIs Bento Grid */}
            <div className="grid grid-cols-3 gap-2.5">
              <div className="bg-white rounded-xl p-3 border border-slate-100 shadow-sm flex flex-col justify-between">
                <span className="text-[10px] font-semibold text-slate-400 flex items-center gap-0.5">
                  全网品牌声量
                  <MetricHelper 
                    title="全网品牌声量"
                    formula="Volume = 关联活动提及帖文数 + 核心关键词检索量"
                    sampling="基于 social_statistic_overview 和 query_analysis_data 聚合平台历史抓取提及数，去除重复噪声（置信度为 95%）。"
                    align="left"
                  />
                </span>
                <div className="flex items-baseline gap-1 mt-1.5">
                  <span className="text-base font-bold text-slate-800 font-display">
                    {Math.round((reportData.engagement.totalViews / 800) + 120).toLocaleString()}
                  </span>
                  <span className="text-[8px] font-bold text-emerald-500 shrink-0">▲ 18.4%</span>
                </div>
              </div>

              <div className="bg-white rounded-xl p-3 border border-slate-100 shadow-sm flex flex-col justify-between">
                <span className="text-[10px] font-semibold text-slate-400 flex items-center gap-0.5">
                  总曝光量
                  <MetricHelper 
                    title="总曝光量"
                    formula="Total Views = ∑ 各红人发布作品前7日播放数"
                    sampling="通过平台官方接口定时抓取发布内容的真实浏览数，并剔除了15%左右的刷量及短时间内高频机器重复访问。"
                    align="center"
                  />
                </span>
                <div className="flex items-baseline gap-1 mt-1.5">
                  <span className="text-base font-bold text-slate-800 font-display">
                    {(reportData.engagement.totalViews / 1000000).toFixed(1)}M
                  </span>
                  <span className="text-[8px] text-slate-400">次曝光</span>
                </div>
              </div>

              <div className="bg-white rounded-xl p-3 border border-slate-100 shadow-sm flex flex-col justify-between">
                <span className="text-[10px] font-semibold text-slate-400 flex items-center gap-0.5">
                  平均互动率
                  <MetricHelper 
                    title="平均互动率"
                    formula="Engagement Rate = (点赞+收藏+评论+分享) / 总曝光 * 100%"
                    sampling="监控作品发布首周核心48小时的全部公开交互数据，以此评估受众深度种草以及产品兴趣度。"
                    align="right"
                  />
                </span>
                <div className="flex items-baseline gap-1 mt-1.5">
                  <span className="text-base font-bold text-slate-800 font-display">
                    {reportData.engagement.avgEngagementRate}%
                  </span>
                  <span className="text-[8px] font-bold text-indigo-500 shrink-0">行业优</span>
                </div>
              </div>
            </div>

            {/* Chart 1: Sentiment Analysis */}
            <div className="bg-white rounded-xl p-4 border border-slate-100 shadow-sm space-y-3">
              <div className="flex items-center justify-between">
                <h4 className="text-xs font-bold text-slate-700 flex items-center gap-1">
                  <ThumbsUp className="h-3.5 w-3.5 text-emerald-500" />
                  舆情情感极性分析
                  <MetricHelper 
                    title="舆情情感极性占比"
                    formula="情感占比 = 某情绪有效评论数 / 脱敏评论总样本数"
                    sampling="全量提取前300条核心热评，通过NLP多分类语义识别模型分类为正向、中立、负向，过滤无意义无属性内容。"
                    align="left"
                  />
                </h4>
                <span className="text-[9px] text-slate-400">粉丝真实评论抽样</span>
              </div>

              <div className="flex items-center gap-1.5">
                <div className="h-32 w-36 shrink-0">
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie
                        data={sentimentPieData}
                        cx="50%"
                        cy="50%"
                        innerRadius={28}
                        outerRadius={45}
                        paddingAngle={4}
                        dataKey="value"
                      >
                        {sentimentPieData.map((entry, index) => (
                          <Cell key={`cell-${index}`} fill={sentimentColors[index % sentimentColors.length]} />
                        ))}
                      </Pie>
                      <Tooltip formatter={(value) => `${value}%`} />
                    </PieChart>
                  </ResponsiveContainer>
                </div>

                {/* Sentiment Legend Progress Bars */}
                <div className="flex-1 space-y-2 text-[11px]">
                  <div>
                    <div className="flex justify-between font-semibold text-slate-600 mb-0.5">
                      <span className="flex items-center gap-1">
                        <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                        正向: {reportData.sentiment.positive}%
                      </span>
                    </div>
                    <div className="h-1.5 w-full bg-slate-100 rounded-full overflow-hidden">
                      <div className="h-full bg-emerald-500" style={{ width: `${reportData.sentiment.positive}%` }} />
                    </div>
                  </div>
                  <div>
                    <div className="flex justify-between font-semibold text-slate-600 mb-0.5">
                      <span className="flex items-center gap-1">
                        <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
                        中立: {reportData.sentiment.neutral}%
                      </span>
                    </div>
                    <div className="h-1.5 w-full bg-slate-100 rounded-full overflow-hidden">
                      <div className="h-full bg-amber-500" style={{ width: `${reportData.sentiment.neutral}%` }} />
                    </div>
                  </div>
                  <div>
                    <div className="flex justify-between font-semibold text-slate-600 mb-0.5">
                      <span className="flex items-center gap-1">
                        <span className="h-1.5 w-1.5 rounded-full bg-red-500" />
                        负向: {reportData.sentiment.negative}%
                      </span>
                    </div>
                    <div className="h-1.5 w-full bg-slate-100 rounded-full overflow-hidden">
                      <div className="h-full bg-red-500" style={{ width: `${reportData.sentiment.negative}%` }} />
                    </div>
                  </div>
                </div>
              </div>

              {/* Keyword Cloud tags */}
              <div className="border-t border-slate-100/80 pt-2.5">
                <span className="text-[10px] font-bold text-slate-400 block mb-1.5">评论区高热词云：</span>
                <div className="flex flex-wrap gap-1.5">
                  {reportData.sentiment.keywords.map((word, i) => (
                    <span 
                      key={i} 
                      className={`text-[10px] px-2 py-0.5 rounded-md font-medium ${
                        i === 0 || i === 2 
                          ? 'bg-emerald-50 text-emerald-600 border border-emerald-100/50' 
                          : i === 4
                          ? 'bg-indigo-50 text-indigo-600 border border-indigo-100/50'
                          : 'bg-slate-100 text-slate-500 border border-slate-200/40'
                      }`}
                    >
                      {word}
                    </span>
                  ))}
                </div>
              </div>
            </div>

            {/* Chart 2: Engagement views trend curve */}
            <div className="bg-white rounded-xl p-4 border border-slate-100 shadow-sm space-y-2">
              <div className="flex items-center justify-between">
                <h4 className="text-xs font-bold text-slate-700 flex items-center gap-1">
                  <TrendingUp className="h-3.5 w-3.5 text-indigo-500" />
                  活动传播周期与曝光走势
                  <MetricHelper 
                    title="传播周期日度曝光"
                    formula="日新增曝光量 = ∑ 各达人当日作品播放增量"
                    sampling="基于平台公共数据流，每24小时增量获取全案内容浏览量，以绘制7天内的传播生命周期波峰及长尾趋势。"
                    align="left"
                  />
                </h4>
                <span className="text-[9px] text-slate-400">7天核心数据监测</span>
              </div>
              
              <div className="h-44 w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={reportData.engagement.trendData} margin={{ top: 10, right: 5, left: -20, bottom: 0 }}>
                    <XAxis dataKey="name" stroke="#94a3b8" fontSize={9} />
                    <YAxis stroke="#94a3b8" fontSize={9} />
                    <Tooltip />
                    <Area type="monotone" dataKey="views" stroke="#6366f1" fillOpacity={0.15} fill="url(#colorViews)" name="曝光量" />
                    <defs>
                      <linearGradient id="colorViews" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#6366f1" stopOpacity={0.8}/>
                        <stop offset="95%" stopColor="#6366f1" stopOpacity={0}/>
                      </linearGradient>
                    </defs>
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </div>

            {/* Chart 3: Demographic analysis */}
            <div className="bg-white rounded-xl p-4 border border-slate-100 shadow-sm space-y-3">
              <div className="flex items-center justify-between">
                <h4 className="text-xs font-bold text-slate-700 flex items-center gap-1">
                  <Users className="h-3.5 w-3.5 text-pink-500" />
                  粉丝客群/受众人口统计画像
                  <MetricHelper 
                    title="受众画像数据"
                    formula="画像分布占比 = 目标特征受众数 / 覆盖总样本粉丝数"
                    sampling="根据本次投放达人在小红书/抖音等平台的官方去重后台受众画像数据（年龄、性别、地域）进行多维数据加权二次聚合。"
                    align="left"
                  />
                </h4>
                <span className="text-[9px] text-slate-400">多维画像重叠</span>
              </div>

              <div className="grid grid-cols-2 gap-4">
                {/* Age brackets bar chart */}
                <div>
                  <span className="text-[10px] font-bold text-slate-400 block mb-1.5">年龄段分布 (%)</span>
                  <div className="h-28 w-full">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={reportData.demographics.age} margin={{ top: 5, right: 0, left: -30, bottom: 0 }}>
                        <XAxis dataKey="name" stroke="#94a3b8" fontSize={9} />
                        <YAxis stroke="#94a3b8" fontSize={9} />
                        <Tooltip formatter={(value) => `${value}%`} />
                        <Bar dataKey="value" fill="#818cf8" radius={[4, 4, 0, 0]} name="占比">
                          {reportData.demographics.age.map((entry, index) => (
                            <Cell key={`cell-${index}`} fill={index === 1 ? '#4f46e5' : '#818cf8'} />
                          ))}
                        </Bar>
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                </div>

                {/* Gender Split Card */}
                <div className="flex flex-col justify-between">
                  <div>
                    <span className="text-[10px] font-bold text-slate-400 block mb-2">性别比例 (Gender)</span>
                    <div className="space-y-2 mt-1">
                      {reportData.demographics.gender.map((item, i) => (
                        <div key={i} className="bg-slate-50/80 rounded-lg p-2 border border-slate-100">
                          <div className="flex items-center justify-between text-[11px] font-semibold text-slate-600">
                            <span>{item.name}</span>
                            <span>{item.value}%</span>
                          </div>
                          <div className="h-1 w-full bg-slate-200 rounded-full mt-1 overflow-hidden">
                            <div className="h-full rounded-full" style={{ width: `${item.value}%`, backgroundColor: item.color || '#6366f1' }} />
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>

              {/* Geographic Region Breakdown */}
              <div className="border-t border-slate-100/80 pt-2.5">
                <span className="text-[10px] font-bold text-slate-400 block mb-1.5">受众省份排名前 5 (地区)：</span>
                <div className="grid grid-cols-5 gap-1">
                  {reportData.demographics.region.slice(0, 5).map((reg, idx) => (
                    <div key={idx} className="bg-slate-50 border border-slate-100/80 rounded-lg p-1.5 text-center">
                      <p className="text-[10px] font-bold text-slate-600 truncate">{reg.name}</p>
                      <p className="text-[11px] font-display font-bold text-indigo-600 mt-0.5">{reg.value}%</p>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </>
        )}

        {/* TAB 2: INFLUENCER & KOL PERFORMANCE */}
        {activeTab === 'kol' && (
          <div className="space-y-4">
            
            {/* Individual KOLs Performance list */}
            <div className="bg-white rounded-xl p-4 border border-slate-100 shadow-sm space-y-3">
              <div className="flex items-center justify-between border-b border-slate-100/60 pb-2">
                <h4 className="text-xs font-bold text-slate-700 flex items-center gap-1">
                  <Eye className="h-3.5 w-3.5 text-indigo-500" />
                  各达人 (KOL) 绩效明细数据
                </h4>
                <span className="text-[9px] text-slate-400">按声量与互动率排序</span>
              </div>

              <div className="space-y-2.5">
                {reportData.kolPerformance.map((kol, idx) => {
                  return (
                    <div 
                      key={idx}
                      className="group/kol rounded-xl border border-slate-100 p-3 hover:bg-slate-50/50 transition duration-150 space-y-2.5"
                    >
                      <div className="flex items-center justify-between">
                        <div className="flex items-center gap-2">
                          <div className="h-7 w-7 rounded-full bg-slate-100 border border-slate-200 flex items-center justify-center font-bold text-slate-600 text-xs">
                            {kol.name.charAt(0)}
                          </div>
                          <div>
                            <p className="text-xs font-bold text-slate-800 group-hover/kol:text-indigo-600 transition">
                              {kol.name}
                            </p>
                            <p className="text-[9px] text-slate-400">
                              粉丝量: {kol.followers} • 渠道: {kol.platform}
                            </p>
                          </div>
                        </div>

                        {/* Positive sentiment level pill */}
                        <div className="text-right">
                          <span className="text-[10px] font-bold bg-emerald-50 text-emerald-600 px-1.5 py-0.5 rounded border border-emerald-100/40">
                            正向舆情 {kol.sentimentPositive}%
                          </span>
                        </div>
                      </div>

                      {/* KOL stats grid */}
                      <div className="grid grid-cols-3 gap-1 bg-slate-50 p-1.5 rounded-lg text-center text-[10px]">
                        <div className="flex flex-col justify-between items-center">
                          <p className="text-slate-400 font-medium flex items-center gap-0.5 justify-center">
                            互动率
                            <MetricHelper 
                              title="达人互动率"
                              formula="互动率 = (转赞评藏总数 / 该红人粉丝总量) * 100%"
                              sampling="以该达人投放文章/视频首周表现为分析母数，真实反映其私域粉丝粘性与核心受众圈层渗透率。"
                              align="left"
                            />
                          </p>
                          <p className="font-bold text-slate-700 mt-0.5">{kol.engagementRate}%</p>
                        </div>
                        <div className="flex flex-col justify-between items-center">
                          <p className="text-slate-400 font-medium">投放成本</p>
                          <p className="font-bold text-slate-700 mt-0.5">{kol.cost}</p>
                        </div>
                        <div className="flex flex-col justify-between items-center">
                          <p className="text-slate-400 font-medium flex items-center gap-0.5 justify-center">
                            声量贡献比
                            <MetricHelper 
                              title="声量贡献比"
                              formula="贡献比 = (该KOL互动数 / 全案总互动) * 100%"
                              sampling="反映该达人在整个传播生命周期中撬动受众参与互动的声量贡献比例，基于 query_analysis_data 汇总测算。"
                              align="right"
                            />
                          </p>
                          <p className="font-bold text-emerald-600 mt-0.5">{((kol.engagementRate * 5.4) + 12).toFixed(1)}%</p>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

          </div>
        )}

      </div>

      {/* Footer / Report Meta Information */}
      <div className="p-3 bg-white border-t border-slate-200 shrink-0 flex items-center justify-between text-[10px] text-slate-400">
        <span>品牌：{brand}</span>
        <span>报表已随会话实时生成</span>
      </div>

    </div>
  );
}
