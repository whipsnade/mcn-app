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

export interface CampaignReportPayload extends AgentArtifactPayloadBase {
  schema_version: 'campaign_report_v2';
  module: 'campaign';
  scope: {
    brand: string;
    campaign: string;
    period: AgentArtifactPeriod | null;
    platforms: string[];
    keywords: string[];
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

export interface KolSelectionScoreSnapshot {
  version: 'kol_score_v2';
  total: number;
  rating: string;
  stars: string;
  data_completeness: number;
  dimensions: Record<string, KolSelectionScoreDimension>;
}

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
  };
  data: {
    scoring: {
      version: 'kol_score_v2';
      method: 'weighted_sum';
      weights: Record<string, number>;
      missing_value_policy: 'missing_as_zero';
    };
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

export type AgentArtifactPayload =
  | BrandReportPayload
  | CampaignReportPayload
  | KolSelectionPayload
  | KolAnalysisPayload
  | KolDetailPayload
  | InsightBoardPayload;

const KNOWN_SCHEMA_VERSIONS = new Set<string>([
  'brand_report_v3',
  'campaign_report_v2',
  'kol_selection_v3',
  'kol_analysis_v2',
  'kol_detail_v2',
  'insight_board_v1',
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
