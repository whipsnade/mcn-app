import React, { useEffect, useState } from 'react';

import { getWallet } from './api/wallet';
import { useAuth } from './auth/AuthProvider';
import AdminPanel from './components/AdminPanel';
import BiReport from './components/BiReport';
import ChatArea from './components/ChatArea';
import LoginPage from './components/LoginPage';
import NewSessionModal, { type NewSessionData } from './components/NewSessionModal';
import RechargeModal from './components/RechargeModal';
import SessionList from './components/SessionList';
import { useWorkspace } from './hooks/useWorkspace';
import type { Account } from './types';


export default function App() {
  const { user, status: authStatus, logout } = useAuth();
  const workspace = useWorkspace(authStatus === 'authenticated');
  const [points, setPoints] = useState(0);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [isNewModalOpen, setIsNewModalOpen] = useState(false);
  const [isRechargeOpen, setIsRechargeOpen] = useState(false);
  const [isAdminOpen, setIsAdminOpen] = useState(false);

  useEffect(() => {
    if (authStatus !== 'authenticated') {
      setPoints(0);
      return;
    }

    let active = true;
    getWallet()
      .then(wallet => {
        if (active) setPoints(wallet.available);
      })
      .catch(() => {
        if (active) setPoints(0);
      });
    return () => {
      active = false;
    };
  }, [authStatus]);

  const handleCreateSession = async (data: NewSessionData) => {
    await workspace.createSession({
      brand: data.brand,
      campaign_name: data.campaignName,
      platforms: data.platforms,
      category: data.category,
      target_audience: data.targetAudience,
      budget_min: data.budgetMin,
      budget_max: data.budgetMax,
      initial_query: data.initialQuery,
    });
  };

  const handleToggleStar = async (id: string) => {
    const session = workspace.sessions.find(item => item.id === id);
    if (!session) return;
    await workspace.updateSession(id, { is_starred: !session.isStarred });
  };

  const handleRenameSession = async (
    id: string,
    brand: string,
    campaignName: string,
  ) => {
    await workspace.updateSession(id, {
      brand,
      campaign_name: campaignName,
      title: `${brand}-${campaignName}`,
    });
  };

  if (authStatus === 'loading') {
    return (
      <div className="flex h-screen items-center justify-center bg-slate-50 text-xs font-medium text-slate-500">
        正在恢复登录状态…
      </div>
    );
  }

  if (!user) {
    return <LoginPage />;
  }

  return (
    <div className="relative flex h-screen w-screen overflow-hidden bg-slate-100 antialiased text-slate-900 font-sans">
      <SessionList
        sessions={workspace.sessions}
        activeSessionId={workspace.activeSessionId ?? ''}
        onSelectSession={id => void workspace.selectSession(id)}
        onOpenNewModal={() => setIsNewModalOpen(true)}
        onToggleStar={id => void handleToggleStar(id)}
        onRenameSession={(id, brand, campaignName) => void handleRenameSession(id, brand, campaignName)}
        user={user}
        onLogout={() => void logout()}
        points={points}
        onOpenRecharge={() => setIsRechargeOpen(true)}
        onOpenAdmin={user.role === 'admin' ? () => setIsAdminOpen(true) : undefined}
      />

      {workspace.activeSession ? (
        <ChatArea
          session={workspace.activeSession}
          onSendMessage={text => void workspace.appendMessage(text)}
          isAnalyzing={workspace.busy}
          isMockMode
        />
      ) : (
        <div className="flex flex-1 items-center justify-center bg-slate-50">
          <div className="text-center">
            <p className="text-xs font-medium text-slate-500">
              {workspace.loading ? '正在加载历史会话…' : '请选择或新建一个 KOL 筛选会话'}
            </p>
            {!workspace.loading && (
              <button
                onClick={() => setIsNewModalOpen(true)}
                className="mt-3 rounded-lg bg-indigo-600 px-3 py-1.5 text-[11px] font-semibold text-white shadow-sm transition hover:bg-indigo-700"
              >
                新建分析会话
              </button>
            )}
          </div>
        </div>
      )}

      <BiReport
        reportData={workspace.activeSession?.reportData}
        campaignName={workspace.activeSession?.campaignName ?? ''}
        brand={workspace.activeSession?.brand ?? ''}
        sessions={workspace.sessions}
      />

      {workspace.error && (
        <div className="absolute bottom-5 left-1/2 z-40 -translate-x-1/2 rounded-xl border border-rose-100 bg-white px-4 py-2 text-xs font-medium text-rose-600 shadow-lg">
          {workspace.error}
        </div>
      )}

      <NewSessionModal
        isOpen={isNewModalOpen}
        onClose={() => setIsNewModalOpen(false)}
        onCreate={handleCreateSession}
      />

      <RechargeModal
        isOpen={isRechargeOpen}
        onClose={() => setIsRechargeOpen(false)}
        onRechargeSuccess={() => setIsRechargeOpen(false)}
        currentPoints={points}
        maxPoints={5000}
      />

      {user.role === 'admin' && (
        <AdminPanel
          isOpen={isAdminOpen}
          onClose={() => setIsAdminOpen(false)}
          accounts={accounts}
          onUpdateAccounts={setAccounts}
          currentUserNickname={user.nickname}
        />
      )}
    </div>
  );
}
