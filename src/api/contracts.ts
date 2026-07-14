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
