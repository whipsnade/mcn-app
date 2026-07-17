export interface ApiToken {
  access_token: string;
  token_type: 'bearer';
}

export interface ApiUser {
  id: string;
  nickname: string;
  role: 'user' | 'admin';
  channels: string[];
}

export interface ApiWallet {
  balance: number;
  reserved: number;
  available: number;
}

export interface ApiMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  sequence: number;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface ApiSession {
  id: string;
  title: string;
  brand: string;
  campaign_name: string | null;
  status: 'draft' | 'analyzing' | 'completed' | 'archived';
  platforms: string[];
  category: string;
  target_audience: string;
  budget_min: string | null;
  budget_max: string | null;
  filters: Record<string, unknown>;
  is_starred: boolean;
  messages: ApiMessage[];
  latest_task?: ApiTaskSummary | null;
  latest_candidates?: ApiCandidateVersionSummary | null;
  latest_report?: ApiBiReportSummary | null;
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

export interface ApiTask {
  id: string;
  session_id: string;
  trigger_message_id?: string | null;
  status: ApiTaskStatus;
  estimated_points: number;
  error_code: string | null;
  error_message?: string | null;
  latest_report_id: string | null;
  followup_suggestions_status?: 'pending' | 'completed' | 'failed' | null;
  followup_suggestions?: FollowupSuggestion[];
  followup_error?: Record<string, unknown> | null;
}

export interface ApiTaskSummary {
  id: string;
  status: ApiTaskStatus;
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

export interface ApiCandidateVersionSummary {
  task_id: string;
  version: number;
  total: number;
}

export interface ApiBiReportSummary {
  id: string;
  task_id: string;
  report_version: number;
  candidate_version: number;
  status: string;
  generated_at: string;
}

export interface ApiCandidate {
  id: string;
  kol_id: string;
  platform: string;
  platform_account_id: string;
  nickname: string | null;
  profile_url: string | null;
  rank: number;
  total_score: number;
  scores: Record<string, number | null>;
  matched_conditions: string[];
  risks: Array<Record<string, unknown>>;
  recommendation: string;
  metrics?: {
    followers: number | null;
    quoted_price_cny: number | null;
    collected_at: string | null;
    data_completeness: number | null;
  };
}

export interface ApiCandidatePage {
  task_id: string;
  version: number;
  total: number;
  items: ApiCandidate[];
}

export interface BiMetric {
  value: number | null;
  unit: string;
  available: boolean;
  coverage: number;
  source_fields: string[];
  platforms: string[];
}

export interface BiDistributionItem {
  label: string;
  value: number;
  unit: string;
}

export interface BiDistribution {
  value: number | null;
  unit: string;
  available: boolean;
  coverage: number;
  source_fields: string[];
  platforms: string[];
  items: BiDistributionItem[];
}

export interface BiSentimentItem {
  key: string;
  label: string;
  value: number;
  percentage: number;
}

export interface BiSentiment {
  available: boolean;
  coverage: number;
  source_fields: string[];
  platforms: string[];
  items: BiSentimentItem[];
  hot_words: Array<{ term: string; count: number }>;
}

export interface BiExposureTrendItem {
  date: string;
  value: number;
  unit: string;
  platforms: string[];
}

export interface BiPeriodTrendItem {
  period: string;
  value: number;
  unit: string;
  platforms: string[];
}

export interface BiAnalyticsData {
  overview: {
    brand_volume: BiMetric;
    total_exposure: BiMetric;
    average_engagement_rate: BiMetric;
  };
  sentiment: BiSentiment;
  exposure_trend: BiExposureTrendItem[];
  volume_trend?: BiPeriodTrendItem[];
  sentiment_trend?: BiPeriodTrendItem[];
  audience: {
    age: BiDistribution;
    gender: BiDistribution;
    regions: BiDistribution;
  };
}

export interface ApiBiReport {
  id: string;
  task_id: string;
  report_version: number;
  candidate_version: number;
  overview: Record<string, unknown>;
  score_composition: Array<Record<string, unknown>>;
  audience_content_fit: Record<string, unknown>;
  platform_distribution: Array<Record<string, unknown>>;
  budget_analysis: Record<string, unknown>;
  comparison: Array<Record<string, unknown>>;
  risks: Array<Record<string, unknown>>;
  analytics?: BiAnalyticsData;
  analysis_scope?: 'brand' | 'kol' | 'hybrid';
  brand_analytics?: BiAnalyticsData;
  kol_analytics?: BiAnalyticsData;
  data_availability?: Record<string, unknown>;
  warnings?: string[];
  conclusion: string;
  sources: Array<Record<string, unknown>>;
  generated_at: string;
}

export interface ApiFavorite {
  kol_id: string;
  nickname?: string | null;
  platform: string;
  platform_account_id: string;
  profile_url: string | null;
  note: string | null;
  source_task_id: string | null;
  created_at: string;
}

export interface CreateSessionInput {
  brand: string;
  campaign_name: string | null;
  platforms: string[];
  category: string;
  target_audience: string;
  budget_min?: string;
  budget_max?: string;
  initial_query: string;
  filters?: Record<string, unknown>;
}
