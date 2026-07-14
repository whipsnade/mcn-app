export interface Message {
  id: string;
  sender: 'user' | 'ai' | 'system';
  text: string;
  timestamp: string;
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
  campaignName: string;
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
