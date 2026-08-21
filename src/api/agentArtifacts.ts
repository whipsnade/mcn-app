/** Agent Artifact 读取 API + 强类型 payload DTO（design §15.2 / Task 10）。
 *
 * 列表 / 详情 / 版本 / 未读水位 / Excel 导出。业务 payload 是
 * ``schema_version`` 判别联合：消费者可按 ``schema_version`` 收窄到具体
 * 类型，核心 ``data`` 是强类型结构而非 ``Record<string, unknown>``。
 */

import { authorizedFetch, request } from './client';


export interface ApiAgentArtifact {
  id: string;
  module: string;
  artifact_type: string;
  parent_artifact_id: string | null;
  artifact_key: string;
  status: string;
  latest_version: number;
  activity_sequence: number;
  created_at: string;
  updated_at: string;
}

export interface ApiAgentArtifactVersion {
  id: string;
  artifact_id: string;
  version: number;
  schema_version: string;
  data_status: string;
  payload: Record<string, unknown> | null;
  evidence_refs: Array<Record<string, unknown>> | null;
  created_at: string;
}

export interface ApiAgentArtifactReadState {
  module: string;
  last_seen_sequence: number;
}

// --------------------------------------------------------------------------- //
// 强类型 payload DTO（镜像 backend/app/agent_artifacts/payloads/*）
// --------------------------------------------------------------------------- //

export interface AgentArtifactSectionAvailability {
  status: 'complete' | 'partial' | 'unavailable';
  reason_codes: string[];
}

export interface AgentArtifactLimitation {
  code: string;
  message: string;
  affected_paths: string[];
}

export interface AgentArtifactMethodology {
  data_as_of: string;
  source_names: string[];
  notes: string[];
}

export interface AgentArtifactPeriod {
  start: string;
  end: string;
  timezone: string;
}

export interface AgentArtifactDistributionItem {
  key: string;
  label: string;
  count: number;
  share: number;
}

export interface AgentArtifactContentTypeItem {
  platform: string;
  type: string;
  posts: number | null;
  volume: number | null;
  engagement: number | null;
}

export interface AgentArtifactSentimentBucket {
  count: number | null;
  share: number | null;
}

export interface AgentArtifactSentimentSummary {
  positive: AgentArtifactSentimentBucket;
  neutral: AgentArtifactSentimentBucket;
  negative: AgentArtifactSentimentBucket;
}

export interface AgentArtifactSentiment {
  summary: AgentArtifactSentimentSummary;
  by_platform: Array<{
    platform: string;
    positive: AgentArtifactSentimentBucket;
    neutral: AgentArtifactSentimentBucket;
    negative: AgentArtifactSentimentBucket;
  }>;
}

export interface AgentArtifactTopPost {
  platform: string;
  post_id: string;
  title: string;
  url: string | null;
  author: string;
  published_at: string;
  likes: number | null;
  comments: number | null;
  shares: number | null;
  engagement: number | null;
}

export interface AgentArtifactFinding {
  title: string;
  detail: string;
  supporting_paths: string[];
}

export interface AgentArtifactRecommendation {
  title: string;
  action: string;
  rationale: string;
  supporting_paths: string[];
}

export interface AgentArtifactPayloadBase {
  data_status: 'complete' | 'restricted';
  availability: Record<string, AgentArtifactSectionAvailability>;
  limitations: AgentArtifactLimitation[];
  methodology: AgentArtifactMethodology;
}

// ---- brand_report_v3 ----

export interface BrandReportPlatformMetric {
  platform: string;
  volume: number | null;
  engagement: number | null;
  posts: number | null;
  share_of_voice: number | null;
  sentiment_score: number | null;
}

export interface BrandReportComparisonMetric {
  metric: string;
  current: number | null;
  baseline: number | null;
  delta: number | null;
  rate: number | null;
}

export interface BrandReportPayload extends AgentArtifactPayloadBase {
  schema_version: 'brand_report_v3';
  module: 'brand';
  scope: {
    brand: string;
    period: AgentArtifactPeriod | null;
    platforms: string[];
    keywords: string[];
    comparison_mode: 'none' | 'mom' | 'mom_yoy';
  };
  data: {
    overview: {
      total_volume: number | null;
      total_engagement: number | null;
      total_posts: number | null;
      sentiment_score: number | null;
      platforms: BrandReportPlatformMetric[];
    };
    comparisons: {
      mom: { status: string; baseline_period: AgentArtifactPeriod | null; metrics: BrandReportComparisonMetric[] };
      yoy: { status: string; baseline_period: AgentArtifactPeriod | null; metrics: BrandReportComparisonMetric[] };
    };
    sentiment: AgentArtifactSentiment;
    daily_trend: Array<{
      date: string;
      platform: string;
      volume: number | null;
      engagement: number | null;
      positive: number | null;
      neutral: number | null;
      negative: number | null;
    }>;
    content_types: AgentArtifactContentTypeItem[];
    creator_tiers: Array<{
      platform: string;
      tier: string;
      creator_count: number | null;
      posts: number | null;
      volume: number | null;
      engagement: number | null;
    }>;
    organic_vs_paid: Array<{
      platform: string;
      kind: string;
      posts: number | null;
      volume: number | null;
      engagement: number | null;
    }>;
    regions: Array<{
      region: string;
      volume: number | null;
      share: number | null;
      sentiment_score: number | null;
    }>;
    topics: Array<{
      topic: string;
      volume: number | null;
      engagement: number | null;
      sentiment_score: number | null;
    }>;
    top_posts: AgentArtifactTopPost[];
  };
  narrative: {
    executive_summary: string;
    findings: AgentArtifactFinding[];
    recommendations: AgentArtifactRecommendation[];
  };
}

// ---- campaign_report_v2 ----

export interface CampaignComparisonSeries {
  metric: string;
  current: number | null;
  baseline: number | null;
  delta: number | null;
  rate: number | null;
}

export interface CampaignAttribution {
  paid_confirmed: number | null;
  organic: number | null;
  unknown: number | null;
  paid_confirmed_share: number | null;
}

export interface CampaignOrganicSummary {
  volume: number | null;
  engagement: number | null;
  posts: number | null;
  share_of_volume: number | null;
}

export interface CampaignAudienceRegion {
  region: string;
  volume: number | null;
  share: number | null;
}

export interface CampaignInternalMetrics {
  spend: number | null;
  impressions: number | null;
  conversions: number | null;
  revenue: number | null;
  cpc: number | null;
  cpm: number | null;
}

export interface CampaignRoi {
  spend: number;
  revenue: number | null;
  conversions: number | null;
  attribution_window: string;
  roi: number | null;
  roas: number | null;
}

export interface CampaignReportPayload extends AgentArtifactPayloadBase {
  schema_version: 'campaign_report_v2';
  module: 'campaign';
  scope: {
    brand: string;
    campaign: string;
    period: AgentArtifactPeriod | null;
    platforms: string[];
    keywords: string[];
    /** Gate C Task 4：排除规则/官方账号/对比模式/归属规则/用户补充资料（历史 Version 可缺失）。 */
    exclusions?: string[];
    official_accounts?: string[];
    comparison_mode?: string | null;
    attribution_rules?: string[];
    upload_ids?: string[];
  };
  data: {
    overview: {
      total_volume: number | null;
      total_engagement: number | null;
      total_posts: number | null;
      total_creators: number | null;
      sentiment_score: number | null;
    };
    platform_contributions: Array<{
      platform: string;
      volume: number | null;
      engagement: number | null;
      posts: number | null;
      creators: number | null;
      share: number | null;
    }>;
    timeline: Array<{
      date: string;
      platform: string;
      volume: number | null;
      engagement: number | null;
      posts: number | null;
    }>;
    kol_contributions: Array<{
      platform: string;
      kol_uid: string;
      nickname: string;
      posts: number | null;
      volume: number | null;
      engagement: number | null;
      contribution_share: number | null;
    }>;
    content_types: AgentArtifactContentTypeItem[];
    sentiment: AgentArtifactSentiment;
    top_posts: AgentArtifactTopPost[];
    /** Gate C Task 4 章节（历史 Version 可缺失；attribution/organic_summary/
     * internal_metrics/roi 后端可输出 null）。 */
    comparisons?: {
      current_baseline: CampaignComparisonSeries[];
      current_post: CampaignComparisonSeries[];
    };
    attribution?: CampaignAttribution | null;
    organic_summary?: CampaignOrganicSummary | null;
    audience_regions?: CampaignAudienceRegion[];
    internal_metrics?: CampaignInternalMetrics | null;
    roi?: CampaignRoi | null;
  };
  narrative: {
    executive_summary: string;
    phase_review: Array<{ phase: string; detail: string; supporting_paths: string[] }>;
    findings: AgentArtifactFinding[];
    recommendations: AgentArtifactRecommendation[];
  };
}

// ---- kol_selection_v3 ----

export interface KolSelectionScoreDimension {
  raw_score: number;
  weight: number;
  weighted_score: number;
  source: string | null;
  missing_reason: string | null;
}

/** 旧版评分快照（只读兼容历史 Version）。 */
export interface KolScoreV2Snapshot {
  version: 'kol_score_v2';
  total: number;
  rating: string;
  stars: string;
  data_completeness: number;
  dimensions: Record<string, KolSelectionScoreDimension>;
}

/** v3 价值评分快照（镜像 backend KolValueScoreSnapshotV3：效果 70 + 价格效率 30）。 */
export interface KolValueScoreV3Snapshot {
  version: 'kol_value_score_v3';
  effect_score: number;
  price_efficiency_score: number;
  value_score: number;
  quoted_price: number | null;
  price_sample_size: number;
  raw_price_efficiency: number | null;
  price_efficiency_percentile: number | null;
  rating: string;
  data_completeness: number;
  dimensions: Record<string, KolSelectionScoreDimension>;
}

export type KolSelectionScoreSnapshot = KolScoreV2Snapshot | KolValueScoreV3Snapshot;

/** 评分配置判别联合（镜像 backend ScoringConfigV2 | ScoringConfigV3）。 */
export type KolScoringConfig =
  | {
      version: 'kol_score_v2';
      method: 'weighted_sum';
      weights: Record<string, number>;
      missing_value_policy: 'missing_as_zero';
    }
  | {
      version: 'kol_value_score_v3';
      method: 'effect_plus_price_efficiency';
      weights: Record<string, number>;
      missing_value_policy: 'missing_as_zero';
    };

export interface KolSelectionItem {
  rank: number;
  platform: string;
  kol_uid: string;
  nickname: string;
  avatar_url: string | null;
  homepage_url: string | null;
  followers: number | null;
  active_followers: number | null;
  active_follower_rate: number | null;
  growth_rate: number | null;
  engagement_total: number | null;
  avg_engagement: number | null;
  likes: number | null;
  comments: number | null;
  shares: number | null;
  quoted_price: number | null;
  reasons: string[];
  missing_fields: string[];
  /** 后端必填 AudienceFilter；历史 Version 可能缺失该键，故前端可选宽松解析。 */
  audience?: { regions: string[]; age_ranges: string[]; interests: string[] };
  score_snapshot: KolSelectionScoreSnapshot;
}

export interface KolSelectionPayload extends AgentArtifactPayloadBase {
  schema_version: 'kol_selection_v3';
  module: 'kol';
  scope: {
    brand: string | null;
    category: string | null;
    campaign: string | null;
    platforms: string[];
    audience: { regions: string[]; age_ranges: string[]; interests: string[] };
    filters: {
      budget_min: number | null;
      budget_max: number | null;
      follower_min: number | null;
      follower_max: number | null;
    };
    /** 本次圈选确认的内容形式（决定图文/视频报价选择）；新版本恒返回，历史版本可能缺失。 */
    content_formats?: string[];
  };
  data: {
    scoring: KolScoringConfig;
    items: KolSelectionItem[];
    summary: {
      candidate_count: number | null;
      selected_count: number | null;
      platform_distribution: AgentArtifactDistributionItem[];
      rating_distribution: AgentArtifactDistributionItem[];
    };
  };
  narrative: {
    selection_summary: string;
    fit_findings: Array<{ text: string; kol_uid: string | null; supporting_paths: string[] }>;
    risk_notes: Array<{ text: string; kol_uid: string | null; supporting_paths: string[] }>;
    usage_advice: Array<{ text: string; kol_uid: string | null; supporting_paths: string[] }>;
  };
}

// ---- kol_analysis_v2 ----

export interface KolAnalysisPayload extends AgentArtifactPayloadBase {
  schema_version: 'kol_analysis_v2';
  module: 'kol';
  scope: {
    selection_artifact_id: string;
    selection_version: string;
    analysis_period: string | null;
  };
  data: {
    summary: {
      kol_count: number | null;
      total_followers: number | null;
      total_active_followers: number | null;
      total_engagement: number | null;
      avg_score: number | null;
    };
    platform_distribution: AgentArtifactDistributionItem[];
    rating_distribution: AgentArtifactDistributionItem[];
    follower_distribution: AgentArtifactDistributionItem[];
    engagement_distribution: AgentArtifactDistributionItem[];
    region_distribution: AgentArtifactDistributionItem[];
    kol_trend: Array<{
      platform: string;
      kol_uid: string;
      nickname: string;
      followers: number | null;
      active_followers: number | null;
      engagement_total: number | null;
      avg_engagement: number | null;
      growth_rate: number | null;
      score: number | null;
    }>;
    top_kols: Array<{
      rank: number;
      platform: string;
      kol_uid: string;
      nickname: string;
      score: number | null;
      engagement_total: number | null;
      rating: string;
    }>;
  };
  narrative: {
    executive_summary: string;
    portfolio_findings: AgentArtifactFinding[];
    mix_recommendations: AgentArtifactFinding[];
    risk_notes: AgentArtifactFinding[];
  };
}

// ---- kol_detail_v2 ----

export interface KolDetailPayload extends AgentArtifactPayloadBase {
  schema_version: 'kol_detail_v2';
  module: 'kol';
  scope: {
    platform: string;
    kol_uid: string;
    selection_artifact_id: string | null;
    selection_version: string | null;
  };
  data: {
    identity: {
      nickname: string;
      avatar_url: string | null;
      homepage_url: string | null;
      bio: string;
      verification: boolean;
      region: string;
    };
    metrics: {
      followers: number | null;
      following: number | null;
      posts: number | null;
      likes: number | null;
      active_followers: number | null;
      active_follower_rate: number | null;
      growth_rate: number | null;
      engagement_total: number | null;
      avg_engagement: number | null;
    };
    audience: {
      gender_distribution: Array<{ key: string; label: string; value: number | null; share: number | null }>;
      age_distribution: Array<{ key: string; label: string; value: number | null; share: number | null }>;
      region_distribution: Array<{ key: string; label: string; value: number | null; share: number | null }>;
      interest_distribution: Array<{ key: string; label: string; value: number | null; share: number | null }>;
    };
    trend: Array<{ date: string; followers: number | null; engagement: number | null; posts: number | null }>;
    latest_posts: AgentArtifactTopPost[];
    cache: { hit: boolean; fetched_at: string; expires_at: string };
  };
  narrative: {
    profile_summary: string;
    content_strengths: AgentArtifactFinding[];
    commercial_notes: AgentArtifactFinding[];
    risk_notes: AgentArtifactFinding[];
  };
}

// ---- insight_board_v1 ----

export interface InsightBoardPayload extends AgentArtifactPayloadBase {
  schema_version: 'insight_board_v1';
  title: string;
  scope: {
    summary: string;
    period: AgentArtifactPeriod | null;
    platforms: string[];
    brand: string | null;
    campaign: string | null;
    kol_uid: string | null;
  };
  parent_artifact_id: string;
  narrative: {
    summary: string;
    findings: AgentArtifactFinding[];
  };
  data: Array<{
    block_type: 'metric_grid' | 'table' | 'bar_chart' | 'line_chart' | 'pie_chart' | 'markdown' | 'timeline' | 'references';
    title: string;
    [key: string]: unknown;
  }>;
}

// ---- analysis_report_v1 ----

export type AnalysisReportColumnType =
  | 'string'
  | 'integer'
  | 'number'
  | 'percent'
  | 'date'
  | 'datetime'
  | 'url'
  | 'boolean';

export type AnalysisReportCell = string | number | boolean | null;

export interface AnalysisReportMetricCard {
  key: string;
  label: string;
  value: AnalysisReportCell;
  unit?: string | null;
  value_type?: AnalysisReportColumnType | null;
}

export interface AnalysisReportMetricCardsBlock {
  block_type: 'metric_cards';
  id: string;
  title: string;
  cards: AnalysisReportMetricCard[];
}

export interface AnalysisReportTableColumn {
  key: string;
  label: string;
  type: AnalysisReportColumnType;
  width?: number | null;
}

export interface AnalysisReportTypedTableBlock {
  block_type: 'typed_table';
  id: string;
  title: string;
  columns: AnalysisReportTableColumn[];
  rows: AnalysisReportCell[][];
}

export interface AnalysisReportTimeSeriesBlock {
  block_type: 'time_series';
  id: string;
  title: string;
  points: Array<{ timestamp: string; values: Record<string, number | null> }>;
}

export interface AnalysisReportLinkListBlock {
  block_type: 'link_list';
  id: string;
  title: string;
  items: Array<{ label: string; url: string; description?: string | null }>;
}

export interface AnalysisReportChartBlock {
  block_type: 'chart';
  id: string;
  title: string;
  chart_type: 'bar' | 'line' | 'area' | 'pie';
  categories: string[];
  series: Array<{ key: string; label: string; values: Array<number | null> }>;
}

export interface AnalysisReportNarrativeBlock {
  block_type: 'narrative';
  id: string;
  title: string;
  content: string;
  supporting_paths: string[];
}

export interface AnalysisReportMethodologyLimitationsBlock {
  block_type: 'methodology_limitations';
  id: string;
  title: string;
  methodology: string;
  limitations: string[];
}

export type AnalysisReportBlock =
  | AnalysisReportMetricCardsBlock
  | AnalysisReportTypedTableBlock
  | AnalysisReportTimeSeriesBlock
  | AnalysisReportLinkListBlock
  | AnalysisReportChartBlock
  | AnalysisReportNarrativeBlock
  | AnalysisReportMethodologyLimitationsBlock;

export interface AnalysisReportFulfillment {
  key: string;
  requested_min: number;
  actual_count: number;
  status: 'complete' | 'partial' | 'unavailable';
  reason: string;
}

export interface AnalysisReportWorkbookSheet {
  key: string;
  title: string;
  block_ids: string[];
  columns: Array<{ key: string; label: string; width?: number | null; number_format?: string | null }>;
  freeze_rows: number;
  auto_filter: boolean;
  sort_by: string[];
  page_size?: number | null;
}

export interface AnalysisReportWorkbookLayout {
  schema_version: 'workbook_v1';
  sheets: AnalysisReportWorkbookSheet[];
}

export interface AnalysisReportPayload extends AgentArtifactPayloadBase {
  schema_version: 'analysis_report_v1';
  module: 'report';
  title: string;
  subject_type: 'brand' | 'campaign' | 'kol' | 'mixed';
  scope: Record<string, unknown>;
  blocks: AnalysisReportBlock[];
  fulfillment: AnalysisReportFulfillment[];
  workbook: AnalysisReportWorkbookLayout | null;
}

export type AgentArtifactPayload =
  | BrandReportPayload
  | CampaignReportPayload
  | KolSelectionPayload
  | KolAnalysisPayload
  | KolDetailPayload
  | InsightBoardPayload
  | AnalysisReportPayload;

const KNOWN_SCHEMA_VERSIONS = new Set<string>([
  'brand_report_v3',
  'campaign_report_v2',
  'kol_selection_v3',
  'kol_analysis_v2',
  'kol_detail_v2',
  'insight_board_v1',
  'analysis_report_v1',
]);

export function isAgentArtifactPayload(value: unknown): value is AgentArtifactPayload {
  if (typeof value !== 'object' || value === null) return false;
  const schemaVersion = (value as { schema_version?: unknown }).schema_version;
  return typeof schemaVersion === 'string' && KNOWN_SCHEMA_VERSIONS.has(schemaVersion);
}

export function getAgentArtifactPayload(
  version: ApiAgentArtifactVersion,
): AgentArtifactPayload | undefined {
  if (version.payload === null || !isAgentArtifactPayload(version.payload)) return undefined;
  return version.payload;
}

// --------------------------------------------------------------------------- //
// 客户端
// --------------------------------------------------------------------------- //

export function listArtifacts(
  sessionId: string,
  module?: string,
  parentArtifactId?: string,
): Promise<ApiAgentArtifact[]> {
  const query = new URLSearchParams();
  if (module) query.set('module', module);
  if (parentArtifactId) query.set('parent_artifact_id', parentArtifactId);
  const suffix = query.toString() ? `?${query.toString()}` : '';
  return request<ApiAgentArtifact[]>(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/artifacts${suffix}`,
  );
}

export function getArtifact(artifactId: string): Promise<ApiAgentArtifact> {
  return request<ApiAgentArtifact>(`/api/v1/agent/artifacts/${encodeURIComponent(artifactId)}`);
}

export function getArtifactVersion(
  artifactId: string,
  version: number,
): Promise<ApiAgentArtifactVersion> {
  return request<ApiAgentArtifactVersion>(
    `/api/v1/agent/artifacts/${encodeURIComponent(artifactId)}/versions/${version}`,
  );
}

export function markArtifactRead(
  sessionId: string,
  module: string,
  lastSeenSequence: number,
): Promise<ApiAgentArtifactReadState> {
  return request<ApiAgentArtifactReadState>(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/artifact-read-state`,
    {
      method: 'PUT',
      body: JSON.stringify({ module, last_seen_sequence: lastSeenSequence }),
    },
  );
}

/** 会话全部模块的服务端已读水位：供 BI 未读圆点初始化（刷新/切换会话不吞离线未读）。 */
export function listArtifactReadStates(sessionId: string): Promise<ApiAgentArtifactReadState[]> {
  return request<ApiAgentArtifactReadState[]>(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/artifact-read-states`,
  );
}

// xlsx 是二进制下载，不能走 request 的 JSON 路径（模式同 reports.ts 的 downloadBrandReport）。
// version 缺省导出最新版本；显式传入时与界面当前查看版本一致（文件名由后端带 _v{N}）。
export async function exportArtifact(artifactId: string, version?: number): Promise<void> {
  const suffix = version !== undefined ? `?version=${version}` : '';
  const response = await authorizedFetch(
    `/api/v1/agent/artifacts/${encodeURIComponent(artifactId)}/export${suffix}`,
  );
  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(body?.detail ?? `HTTP_${response.status}`);
  }
  const disposition = response.headers.get('Content-Disposition') ?? '';
  const filenameStar = /filename\*=UTF-8''([^;]+)/.exec(disposition);
  const plain = /filename="([^"]+)"/.exec(disposition);
  const filename = filenameStar
    ? decodeURIComponent(filenameStar[1])
    : plain
      ? plain[1]
      : 'artifact.xlsx';
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
