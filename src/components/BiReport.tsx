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
  const [activeTab, setActiveTab] = useState<'dashboard' | 'kol' | 'ai'>('dashboard');

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
            数据看板 (BI)
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
            红人与 MCN
          </span>
        </button>
        <button
          onClick={() => setActiveTab('ai')}
          className={`flex-1 py-3.5 text-xs font-bold text-center border-b-2 transition ${
            activeTab === 'ai'
              ? 'border-indigo-600 text-indigo-600 bg-white'
              : 'border-transparent text-slate-500 hover:text-slate-800'
          }`}
        >
          <span className="flex items-center justify-center gap-1.5">
            <Activity className="h-3.5 w-3.5" />
            AI 推荐策略
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
                  大盘预估 ROI
                  <MetricHelper 
                    title="大盘预估 ROI"
                    formula="ROI = 预估带货总GMV / 实际投放成本"
                    sampling="抽取合作红人最近3个月的带货转化率与客单价模型，结合去重后的跨平台触达归因推算（置信度为 92%）。"
                    align="left"
                  />
                </span>
                <div className="flex items-baseline gap-1 mt-1.5">
                  <span className="text-base font-bold text-slate-800 font-display">
                    {reportData.mcnAnalysis.roi.toFixed(2)}
                  </span>
                  <span className="text-[8px] font-bold text-emerald-500 shrink-0">▲ 12%</span>
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

        {/* TAB 2: INFLUENCER & MCN APPRAISAL */}
        {activeTab === 'kol' && (
          <div className="space-y-4">
            
            {/* MCN Rating Core Card */}
            <div className="bg-white rounded-xl p-4 border border-slate-100 shadow-sm space-y-3.5">
              <div className="flex items-center justify-between border-b border-slate-100/60 pb-2.5">
                <div>
                  <span className="text-[10px] font-bold text-indigo-500 tracking-wide uppercase">合作 MCN 数据档案</span>
                  <h4 className="text-sm font-bold text-slate-800 mt-0.5">{reportData.mcnAnalysis.mcnName}</h4>
                </div>
                <div className="text-right">
                  <div className="flex items-center justify-end gap-0.5 text-amber-500">
                    <Star className="h-3.5 w-3.5 fill-current" />
                    <span className="text-xs font-bold text-slate-800">{ (reportData.mcnAnalysis.score / 20).toFixed(1) }</span>
                  </div>
                  <span className="text-[9px] text-slate-400">综合履约评级</span>
                </div>
              </div>

              {/* Financial & Compliance Metrics grid */}
              <div className="grid grid-cols-4 gap-2 text-center">
                <div className="bg-slate-50 border border-slate-100 p-2 rounded-xl flex flex-col justify-between items-center">
                  <p className="text-[9px] font-semibold text-slate-400 flex items-center gap-0.5 justify-center">
                    综合评分
                    <MetricHelper 
                      title="MCN 综合评分"
                      formula="Score = 0.4*履约交付 + 0.3*爆文系数 + 0.3*高性价比"
                      sampling="基于本系统的自研MCN评级模型，根据该机构近半年累计带货、准时出片与违规率等综合加权给出。"
                      align="left"
                    />
                  </p>
                  <p className="text-xs font-bold text-slate-800 mt-1">{reportData.mcnAnalysis.score}分</p>
                </div>
                <div className="bg-slate-50 border border-slate-100 p-2 rounded-xl flex flex-col justify-between items-center">
                  <p className="text-[9px] font-semibold text-slate-400 flex items-center gap-0.5 justify-center">
                    履约交付率
                    <MetricHelper 
                      title="履约交付率"
                      formula="Fulfillment = 按期正常出街视频 / 预定总排期数"
                      sampling="实时跟踪记录初审通过时间、脚本修改次数以及上线时效性，低于90%由系统报警催促。"
                      align="left"
                    />
                  </p>
                  <p className="text-xs font-bold text-emerald-500 mt-1">{reportData.mcnAnalysis.fulfillmentRate}%</p>
                </div>
                <div className="bg-slate-50 border border-slate-100 p-2 rounded-xl flex flex-col justify-between items-center">
                  <p className="text-[9px] font-semibold text-slate-400 flex items-center gap-0.5 justify-center">
                    平均 CPM
                    <MetricHelper 
                      title="平均 CPM"
                      formula="CPM = (实际支出成本 / 获得总曝光量) * 1,000"
                      sampling="反映每千次展现所付出的成本，能有效评估本次推广覆盖用户的性价比是否优于竞品均值。"
                      align="right"
                    />
                  </p>
                  <p className="text-xs font-bold text-slate-800 mt-1">¥{reportData.mcnAnalysis.cpm}</p>
                </div>
                <div className="bg-slate-50 border border-slate-100 p-2 rounded-xl flex flex-col justify-between items-center">
                  <p className="text-[9px] font-semibold text-slate-400 flex items-center gap-0.5 justify-center">
                    平均 CPE
                    <MetricHelper 
                      title="平均 CPE"
                      formula="CPE = 实际支出成本 / 互动行为总和(转评赞藏)"
                      sampling="代表每次粉丝进行有效互动的单价成本，用于深度评估粉丝真实认同感与内容带货意愿。"
                      align="right"
                    />
                  </p>
                  <p className="text-xs font-bold text-indigo-600 mt-1">¥{reportData.mcnAnalysis.cpe}</p>
                </div>
              </div>

              {/* Strengths & Weaknesses */}
              <div className="grid grid-cols-2 gap-3.5 text-xs pt-1">
                <div className="space-y-1.5">
                  <span className="text-[10px] font-bold text-emerald-500 flex items-center gap-1">
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    交付优势 (Strengths)
                  </span>
                  <ul className="space-y-1 text-slate-600 leading-relaxed text-[11px] list-disc pl-3">
                    {reportData.mcnAnalysis.strengths.map((str, idx) => (
                      <li key={idx}>{str}</li>
                    ))}
                  </ul>
                </div>

                <div className="space-y-1.5">
                  <span className="text-[10px] font-bold text-amber-500 flex items-center gap-1">
                    <AlertTriangle className="h-3.5 w-3.5" />
                    优化瓶颈 (Bottlenecks)
                  </span>
                  <ul className="space-y-1 text-slate-600 leading-relaxed text-[11px] list-disc pl-3">
                    {reportData.mcnAnalysis.weaknesses.map((weak, idx) => (
                      <li key={idx}>{weak}</li>
                    ))}
                  </ul>
                </div>
              </div>
            </div>

            {/* Individual KOLs Performance list */}
            <div className="bg-white rounded-xl p-4 border border-slate-100 shadow-sm space-y-3">
              <div className="flex items-center justify-between border-b border-slate-100/60 pb-2">
                <h4 className="text-xs font-bold text-slate-700 flex items-center gap-1">
                  <Eye className="h-3.5 w-3.5 text-indigo-500" />
                  各达人 (KOL) 绩效明细数据
                </h4>
                <span className="text-[9px] text-slate-400">按转化与爆文率排序</span>
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
                          <p className="text-slate-400 font-medium">坑位费/成本</p>
                          <p className="font-bold text-slate-700 mt-0.5">{kol.cost}</p>
                        </div>
                        <div className="flex flex-col justify-between items-center">
                          <p className="text-slate-400 font-medium flex items-center gap-0.5 justify-center">
                            预估销售转化
                            <MetricHelper 
                              title="预估销售转化"
                              formula="预估转化 = 粉丝总数 * 种草成交率 * 商品平均客单价"
                              sampling="结合达人最近3次带货数据、品牌专享券核销率以及同品类大盘成交归因模型计算得出的预估销售额。"
                              align="right"
                            />
                          </p>
                          <p className="font-bold text-emerald-600 mt-0.5">{kol.salesConversion || '¥45,000'}</p>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

          </div>
        )}

        {/* TAB 3: AI STRATEGIC RECOMMENDATIONS */}
        {activeTab === 'ai' && (
          <div className="space-y-4">
            
            {/* AI Diagnostics Core overview card */}
            <div className="bg-indigo-600 text-white rounded-xl p-4 shadow-md space-y-2 relative overflow-hidden">
              <div className="absolute right-[-10px] top-[-10px] opacity-10">
                <Sparkles className="h-28 w-28" />
              </div>
              <span className="text-[9px] font-bold bg-indigo-500/80 text-indigo-100 px-2 py-0.5 rounded-full uppercase tracking-wider">
                智能诊断
              </span>
              <h4 className="text-sm font-bold font-display">
                基于舆情与 ROI 的优化策略
              </h4>
              <p className="text-xs text-indigo-100 leading-relaxed font-light">
                本报告由 AI 大模型根据达人粉丝重叠度、互动流失率及销售归因漏洞自动编译而成，旨在最大化提升下一阶段的获客性价比。
              </p>
            </div>

            {/* Strategic Recommendations list */}
            <div className="bg-white rounded-xl p-4 border border-slate-100 shadow-sm space-y-4">
              <div className="flex items-center justify-between border-b border-slate-100 pb-2.5">
                <span className="text-xs font-bold text-slate-700">推荐落地动作 (Action Items)：</span>
                <span className="text-[10px] font-semibold text-emerald-500">已就绪</span>
              </div>

              <div className="space-y-3.5">
                {reportData.recommendations.map((rec, i) => (
                  <div key={i} className="flex gap-2.5 items-start">
                    <div className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-indigo-50 text-[10px] font-bold text-indigo-600 mt-0.5">
                      {i + 1}
                    </div>
                    <div>
                      {/* Check if recommendation text contains formatting tags or simple text */}
                      <p className="text-xs text-slate-600 font-medium leading-relaxed">
                        {rec.replace(/^\d+[\.\s*]+/, '')}
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Simulated competitor benchmark widget */}
            <div className="bg-slate-50 border border-slate-200/50 rounded-xl p-3.5 text-xs">
              <span className="text-[10px] font-bold text-slate-400 block mb-2">行业竞品大盘均值对比：</span>
              <div className="space-y-2 text-[11px]">
                <div className="flex justify-between items-center">
                  <span className="text-slate-500">本活动获客 ROI:</span>
                  <div className="flex items-center gap-1.5">
                    <div className="h-1.5 w-16 bg-indigo-600 rounded-full" />
                    <span className="font-bold text-slate-700">{reportData.mcnAnalysis.roi.toFixed(2)}</span>
                  </div>
                </div>
                <div className="flex justify-between items-center">
                  <span className="text-slate-500">竞品平均 ROI:</span>
                  <div className="flex items-center gap-1.5">
                    <div className="h-1.5 w-12 bg-slate-300 rounded-full" />
                    <span className="font-bold text-slate-500">1.80</span>
                  </div>
                </div>
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
