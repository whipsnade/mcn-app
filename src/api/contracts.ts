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
  campaign_name: string;
  status: 'draft' | 'analyzing' | 'completed' | 'archived';
  platforms: string[];
  category: string;
  target_audience: string;
  budget_min: string | null;
  budget_max: string | null;
  filters: Record<string, unknown>;
  is_starred: boolean;
  messages: ApiMessage[];
  created_at: string;
  updated_at: string;
}

export interface CreateSessionInput {
  brand: string;
  campaign_name: string;
  platforms: string[];
  category: string;
  target_audience: string;
  budget_min?: string;
  budget_max?: string;
  initial_query: string;
  filters?: Record<string, unknown>;
}
