export interface Message {
  id: string;
  sender: 'user' | 'ai' | 'system';
  text: string;
  timestamp: string;
  taskId?: string;
}

export interface SentimentData {
  positive: number;
  neutral: number;
  negative: number;
  keywords: string[];
}

export interface EngagementTrend {
  name: string;
  views: number;
  engagement: number;
}

export interface EngagementData {
  totalViews: number;
  totalLikes: number;
  totalComments: number;
  totalShares: number;
  avgEngagementRate: number; // percentage
  trendData: EngagementTrend[];
}

export interface DemographicSegment {
  name: string;
  value: number;
  color?: string;
}

export interface DemographicsData {
  gender: DemographicSegment[];
  age: DemographicSegment[];
  region: DemographicSegment[];
}

export interface MCNAnalysisData {
  mcnName: string;
  fulfillmentRate: number; // percentage
  cpm: number; // Cost Per Mille (CNY)
  cpe: number; // Cost Per Engagement (CNY)
  roi: number; // Return on Investment (ratio e.g. 2.4)
  score: number; // Score out of 100
  strengths: string[];
  weaknesses: string[];
}

export interface KOLPerformance {
  name: string;
  avatar: string;
  platform: 'Xiaohongshu' | 'Douyin' | 'Bilibili' | 'Weibo' | 'Instagram' | 'YouTube';
  followers: string;
  engagementRate: number; // percentage
  cost: string;
  salesConversion?: string;
  sentimentPositive: number; // percentage
}

export interface ReportData {
  sentiment: SentimentData;
  engagement: EngagementData;
  demographics: DemographicsData;
  mcnAnalysis: MCNAnalysisData;
  kolPerformance: KOLPerformance[];
  recommendations: string[];
}

export interface Session {
  id: string;
  title: string;
  brand: string;
  campaignName: string | null;
  status: 'completed' | 'analyzing' | 'draft' | 'archived';
  platform: string;
  category: string;
  targetAudience: string;
  budgetMin?: string;
  budgetMax?: string;
  summary: string;
  messages: Message[];
  reportData?: ReportData;
  isStarred: boolean;
  createdAt: string;
  updatedAt: string;
  mcn?: string;
  kols?: string[];
  analysis?: SessionAnalysis;
  candidates?: KolCandidate[];
  biReport?: AnalysisBiReport;
}

export interface SessionAnalysis {
  taskId: string;
  status: string;
  candidateVersion?: number;
  reportId?: string;
  followupStatus?: 'pending' | 'completed' | 'failed';
  followupSuggestions?: import('./api/contracts').FollowupSuggestion[];
  followupError?: Record<string, unknown>;
}

export interface KolCandidate {
  id: string;
  kolId: string;
  platform: string;
  platformAccountId: string;
  nickname?: string;
  profileUrl?: string;
  rank: number;
  totalScore: number;
  scores: Record<string, number | null>;
  matchedConditions: string[];
  risks: Array<Record<string, unknown>>;
  recommendation: string;
  metrics?: {
    followers: number | null;
    quoted_price_cny: number | null;
    collected_at: string | null;
    data_completeness: number | null;
  };
}

export interface AnalysisBiReport {
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
  analytics?: Record<string, unknown>;
  conclusion: string;
  sources: Array<Record<string, unknown>>;
  generated_at: string;
}

export interface Account {
  id: string;
  username: string;
  phone: string;
  channels: string[];
  points: number;
  role: 'admin' | 'user';
  createdAt: string;
}
