export interface ApiToken {
  access_token: string;
  token_type: 'bearer';
}

export interface ApiUser {
  id: string;
  nickname: string;
  role: 'user' | 'admin';
  channels: string[];
  industries: string[];
}

export interface ApiWallet {
  balance: number;
  reserved: number;
  available: number;
}

export interface ApiAdminUser {
  id: string;
  nickname: string;
  role: 'user' | 'admin';
  status: 'active' | 'disabled';
  phone: string | null;
  points: number;
  reserved_points: number;
  channels: string[];
  industries: string[];
  created_at: string;
}

export interface ApiAdminUserList {
  items: ApiAdminUser[];
  total: number;
}

export interface ApiAdminUserCreateInput {
  nickname: string;
  phone: string;
  role: 'user' | 'admin';
  points?: number;
  channels?: string[];
  industries?: string[];
}

export interface ApiAdminUserUpdateInput {
  nickname?: string;
  phone?: string;
  role?: 'user' | 'admin';
  status?: 'active' | 'disabled';
  channels?: string[];
  industries?: string[];
}

export interface ApiAdminPointsAdjustResult {
  points: number;
  reserved_points: number;
  transaction_id: string;
}

export interface ApiPointsHistoryEntry {
  id: string;
  kind: string;
  points: number;
  session_title: string | null;
  platform: string | null;
  created_at: string;
}

export interface ApiPointsHistory {
  items: ApiPointsHistoryEntry[];
  total: number;
}

export interface ApiBrainstormPeriod {
  start: string;
  end: string;
}

export interface ApiBrainstormProfile {
  brand: string | null;
  category: string | null;
  platforms: string[];
  audience: string | null;
  period: ApiBrainstormPeriod | null;
  kol_filters: string | null;
  goal: string | null;
}

export interface BrainstormMetadata {
  ready: boolean;
  options: string[];
  profile_summary?: ApiBrainstormProfile | null;
  /** 平台问题等允许多选的澄清选项；缺省/false 为单选（点击即填入）。 */
  multi?: boolean;
}

export interface ClarifyMetadata {
  options: string[];
}

export type ApiThinkingBlockStatus = 'running' | 'completed' | 'interrupted';

export interface ApiThinkingBlock {
  operation_id: string;
  purpose: string;
  attempt: number;
  label: string;
  content: string;
  status: ApiThinkingBlockStatus;
  started_at?: string;
  completed_at?: string;
  duration_ms?: number;
  task_id?: string | null;
  goal_id?: string | null;
  truncated?: boolean;
  error_code?: string | null;
}

export interface ApiThinkingMetadata {
  version: 1;
  status: ApiThinkingBlockStatus;
  blocks: ApiThinkingBlock[];
}

export type ApiSessionThinkingEventType =
  | 'thinking.started'
  | 'thinking.delta'
  | 'thinking.snapshot'
  | 'thinking.completed'
  | 'thinking.failed';

export interface ApiSessionThinkingEventPayload {
  operation_id: string;
  turn_id: string;
  session_id: string;
  purpose: string;
  attempt: number;
  label: string;
  sequence: number;
  task_id?: string | null;
  goal_id?: string | null;
  trigger_message_id?: string | null;
  text?: string;
  status?: ApiThinkingBlockStatus;
  duration_ms?: number;
  error_code?: string | null;
  truncated?: boolean;
}

export interface ApiMessageMetadata extends Record<string, unknown> {
  turn_id?: string;
  brainstorm?: BrainstormMetadata;
  clarify?: ClarifyMetadata;
  thinking?: ApiThinkingMetadata;
}

export interface ApiMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  sequence: number;
  metadata: ApiMessageMetadata;
  created_at: string;
}

export interface ApiBrainstormResponse {
  ready: boolean;
  task_id: string | null;
  message: ApiMessage;
  profile: ApiBrainstormProfile;
}

export interface ApiSession {
  id: string;
  title: string;
  brand: string | null;
  campaign_name: string | null;
  status: 'draft' | 'analyzing' | 'completed' | 'archived';
  platforms: string[];
  category: string | null;
  target_audience: string;
  budget_min: string | null;
  budget_max: string | null;
  filters: Record<string, unknown>;
  is_starred: boolean;
  messages: ApiMessage[];
  kol_selection_count: number;
  latest_task?: ApiTaskSummary | null;
  latest_analysis_report?: ApiAnalysisReportSummary | null;
  created_at: string;
  updated_at: string;
}

export type ApiTaskStatus =
  | 'pending'
  | 'planning'
  | 'running'
  | 'completed'
  | 'completed_with_warnings'
  | 'failed'
  | 'insufficient_balance'
  | 'interrupted'
  | 'cancelled';

export type ApiTaskKind = 'pipeline' | 'agent';

export interface ApiTask {
  id: string;
  session_id: string;
  trigger_message_id?: string | null;
  status: ApiTaskStatus;
  kind?: ApiTaskKind;
  estimated_points: number;
  error_code: string | null;
  error_message?: string | null;
  latest_report_id: string | null;
  followup_suggestions_status?: 'pending' | 'completed' | 'failed' | null;
  followup_suggestions?: FollowupSuggestion[];
  followup_error?: Record<string, unknown> | null;
}

/** create_task 已建任务（enforce 关闭或 planner execute）。 */
export interface ApiTaskCreateTaskOutcome {
  outcome: 'task';
  task: ApiTask;
}

/** create_task planner 澄清：不落任务，返回 assistant 澄清消息。 */
export interface ApiTaskCreateClarifyOutcome {
  outcome: 'clarify';
  message: ApiMessage;
}

/** create_task planner 对话式回复：不落任务，返回 assistant 回复消息。 */
export type ApiRespondType = 'context_qa' | 'usage_help' | 'out_of_scope';

export interface ApiTaskCreateRespondOutcome {
  outcome: 'respond';
  respond_type: ApiRespondType;
  message: ApiMessage;
}

export type TaskCreateResult =
  | ApiTaskCreateTaskOutcome
  | ApiTaskCreateClarifyOutcome
  | ApiTaskCreateRespondOutcome;

export interface ApiTaskSummary {
  id: string;
  status: ApiTaskStatus;
  kind?: ApiTaskKind;
  completed_at: string | null;
  followup_suggestions_status?: 'pending' | 'completed' | 'failed' | null;
  followup_suggestions?: FollowupSuggestion[];
  followup_error?: Record<string, unknown> | null;
}

export interface FollowupSuggestion {
  title: string;
  prompt: string;
  rationale: string;
}

export interface ApiAnalysisReportMetricItem {
  label: string;
  value: string | number;
  unit?: string;
  delta?: string;
}

export interface ApiAnalysisReportChartSeries {
  name: string;
  values: (number | null)[];
}

export type ReportBlock =
  | { type: 'heading'; text: string }
  | { type: 'markdown'; text: string }
  | { type: 'metric_grid'; title?: string; items: ApiAnalysisReportMetricItem[] }
  | { type: 'table'; title?: string; columns: string[]; rows: (string | number | null)[][] }
  | { type: 'bar_chart'; title?: string; categories: string[]; series: ApiAnalysisReportChartSeries[] }
  | { type: 'line_chart'; title?: string; categories: string[]; series: ApiAnalysisReportChartSeries[] }
  | { type: 'pie_chart'; title?: string; categories: string[]; series: ApiAnalysisReportChartSeries[] }
  | { type: 'tag_list'; title?: string; items: string[] }
  | { type: 'sources'; items: Array<{ name: string; collected_at?: string; evidence?: string }> };

// ---- brand_report_v2 结构化快照契约（镜像 backend/app/reporting/brand_payload.py）----

export type BrandReportPeriodStatus = 'ok' | 'not_requested' | 'restricted';

export interface BrandReportPeriodValue {
  value: number | null;
  status: BrandReportPeriodStatus;
  reason?: string | null;
}

export interface BrandReportChapterAvailability {
  status: 'complete' | 'partial' | 'unavailable';
  missing_fields: string[];
  reason?: string | null;
  source_tools: string[];
  collected_at?: string | null;
}

export interface BrandReportScope {
  brand: string;
  period_start: string | null;
  period_end: string | null;
  platforms: string[];
  comparison_mode: 'mom' | 'mom_yoy';
  data_as_of?: string | null;
}

export interface BrandReportQuerySpec {
  original_term: string;
  matched_tag?: string | null;
  fallback_keyword?: string | null;
  comparison_definition: string;
}

export interface BrandReportSourceEntry {
  tool: string;
  collected_at?: string | null;
  step_id?: string | null;
}

export interface BrandReportPlatformOverview {
  platform: string;
  mentions: number | null;
  interactions: number | null;
}

export interface BrandReportMetricComparison {
  current: number | null;
  mom: BrandReportPeriodValue;
  yoy: BrandReportPeriodValue;
  mom_change_pct: number | null;
  yoy_change_pct: number | null;
}

export interface BrandReportSentimentSplit {
  positive: number | null;
  neutral: number | null;
  negative: number | null;
}

export interface BrandReportOverviewSection {
  platforms: BrandReportPlatformOverview[];
  total_mentions: BrandReportMetricComparison;
  total_interactions: BrandReportMetricComparison;
  sentiment_split: BrandReportSentimentSplit;
}

export interface BrandReportSentimentRow {
  platform: string;
  sentiment: string;
  mentions: number | null;
  interactions: number | null;
  share_pct: number | null;
}

export interface BrandReportTrendPoint {
  date: string;
  mentions: number | null;
  interactions: number | null;
}

export interface BrandReportDailyTrendSection {
  points: BrandReportTrendPoint[];
  peak_date: string | null;
  peak_mentions: number | null;
}

export interface BrandReportContentTypeRow {
  content_type: string;
  mentions: number | null;
  share_pct: number | null;
}

export interface BrandReportCreatorTierRow {
  tier: string;
  mentions: number | null;
  share_pct: number | null;
}

export interface BrandReportOrganicVsPaid {
  organic_mentions: number | null;
  paid_mentions: number | null;
  organic_share_pct: number | null;
  paid_share_pct: number | null;
}

export interface BrandReportRegionRow {
  region: string;
  mentions: number | null;
  interactions: number | null;
  share_pct: number | null;
}

export interface BrandReportTopPost {
  platform: string;
  post_id?: string | null;
  collected_at?: string | null;
  title: string | null;
  author: string | null;
  interactions: number | null;
  /** 小红书=阅读数 / 抖音=播放数 */
  exposure_count: number | null;
  like_count: number | null;
  comment_count: number | null;
  collect_count: number | null;
  /** 小红书=转发 / 抖音=分享 */
  share_count: number | null;
  sentiment: string | null;
  creator_tier: string | null;
  url: string | null;
}

export interface BrandReportData {
  overview: BrandReportOverviewSection;
  sentiment: { rows: BrandReportSentimentRow[] };
  daily_trend: BrandReportDailyTrendSection;
  content_types: BrandReportContentTypeRow[];
  creator_tiers: BrandReportCreatorTierRow[];
  organic_vs_paid: BrandReportOrganicVsPaid;
  regions: BrandReportRegionRow[];
  top_posts: BrandReportTopPost[];
}

export interface BrandReportNarrative {
  praise_points: string[];
  complaint_points: string[];
  impact_level: '低' | '中' | '高';
  expansion_signals: string[];
  noise_notes?: string | null;
  key_findings: string[];
  conclusion: string;
  recommendations: string[];
}

export interface BrandReportPayload {
  template_version: 'brand_report_v2';
  data_status: 'complete' | 'partial';
  scope: BrandReportScope;
  query_spec: BrandReportQuerySpec;
  data: BrandReportData;
  narrative?: BrandReportNarrative | null;
  /** 8 章节键：overview/sentiment/daily_trend/content_creators/regions/top_posts/insights/methodology */
  availability: Record<string, BrandReportChapterAvailability>;
  sources: BrandReportSourceEntry[];
}

export interface ApiAnalysisReport {
  id: string;
  // 会话级 KOL 分析报告不绑定任务，task_id 为 null。
  task_id: string | null;
  report_type?: string;
  scope?: Record<string, unknown> | null;
  version: number;
  title: string;
  blocks: ReportBlock[];
  conclusion: string | null;
  status: string;
  // brand_report_v2 结构化快照，仅新品牌报告返回，旧报告为 null。
  payload?: BrandReportPayload | null;
  template_version?: string | null;
  generated_at: string;
}

export interface ApiSessionReportItem {
  report_id: string;
  title: string;
  version: number;
  scope: Record<string, unknown> | null;
  status: string;
  created_at: string;
}

export type ArtifactModuleKey = 'brand' | 'campaign' | 'kol_analysis' | 'kol_selection';

export interface ApiArtifactSummaryEntry {
  artifact_id: string;
  artifact_type: string;
  title: string;
  version: number;
  scope: Record<string, unknown> | null;
  status: string;
  created_at: string;
}

export interface ApiArtifactModuleSummary {
  latest_artifact: ApiArtifactSummaryEntry | null;
  unread: boolean;
}

export type ApiArtifactsSummary = Record<ArtifactModuleKey, ApiArtifactModuleSummary>;

export interface ApiSelectionSetItem {
  set_id: string;
  title: string;
  version: number;
  status: string;
  item_count: number;
  created_at: string;
}

export interface ApiAnalysisReportSummary {
  id: string;
  // 会话级 KOL 分析报告不绑定任务，task_id 为 null。
  task_id: string | null;
  version: number;
  title: string;
  status: string;
  generated_at: string;
}

export interface ApiFavorite {
  id: string;
  // 新路径（platform+kol_uid）收藏的行的 kol_id / platform_account_id 为 null。
  kol_id: string | null;
  nickname?: string | null;
  platform: string;
  platform_account_id: string | null;
  kol_uid: string | null;
  profile_url: string | null;
  snapshot: Record<string, unknown> | null;
  note: string | null;
  source_task_id: string | null;
  created_at: string;
}

export interface CreateSessionInput {
  brand?: string;
  campaign_name?: string | null;
  platforms?: string[];
  category?: string;
  target_audience?: string;
  budget_min?: string;
  budget_max?: string;
  initial_query?: string;
  filters?: Record<string, unknown>;
}

export type ApiQuickPlatform = 'xiaohongshu' | 'douyin';

export interface ApiQuickKolItem {
  platform: ApiQuickPlatform | string;
  kw_uid: string;
  nickname: string;
  fans: number | null;
  price: number | null;
  engagement_rate: number | null;
  score: number | null;
  city: string | null;
  tags: string[];
}

export interface ApiQuickKolRecommendations {
  items: ApiQuickKolItem[];
  points_cost: number;
}

export interface ApiQuickKolDetail {
  detail: Record<string, unknown>;
  posts: Array<Record<string, unknown>>;
  points_cost: number;
  posts_degraded?: boolean;
}

export interface ApiQuickTopPost {
  title: string;
  nickname: string;
  interact: number | null;
  like: number | null;
  comment: number | null;
  collect: number | null;
  publish_time: string | null;
  url: string | null;
  platform: string;
}

export interface ApiQuickTopPosts {
  items: ApiQuickTopPost[];
  points_cost: number;
  degraded?: boolean;
  fallback_kols?: ApiQuickKolItem[];
}

export interface ApiQuickEvaluateResult {
  title: string;
  analysis_markdown: string;
}
