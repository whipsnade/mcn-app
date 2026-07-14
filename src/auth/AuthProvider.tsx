import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';

import * as authApi from '../api/auth';
import type { ApiUser } from '../api/contracts';

type AuthStatus = 'loading' | 'authenticated' | 'anonymous';

interface AuthContextValue {
  status: AuthStatus;
  user: ApiUser | null;
  loginWithSms: (phone: string, code: string) => Promise<void>;
  loginWithWechat: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [user, setUser] = useState<ApiUser | null>(null);

  useEffect(() => {
    let active = true;
    authApi.restoreSession()
      .then(restoredUser => {
        if (!active) return;
        setUser(restoredUser);
        setStatus(restoredUser ? 'authenticated' : 'anonymous');
      })
      .catch(() => {
        if (!active) return;
        setUser(null);
        setStatus('anonymous');
      });
    return () => {
      active = false;
    };
  }, []);

  const value = useMemo<AuthContextValue>(() => ({
    status,
    user,
    async loginWithSms(phone, code) {
      const nextUser = await authApi.loginWithSms(phone, code);
      setUser(nextUser);
      setStatus('authenticated');
    },
    async loginWithWechat() {
      const nextUser = await authApi.loginWithWechat();
      setUser(nextUser);
      setStatus('authenticated');
    },
    async logout() {
      await authApi.logout();
      setUser(null);
      setStatus('anonymous');
    },
  }), [status, user]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used inside AuthProvider');
  return context;
}
