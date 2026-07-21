import React, { useCallback, useEffect, useState } from 'react';

import type { ApiFavorite } from './api/contracts';
import { listFavorites } from './api/favorites';
import { getWallet } from './api/wallet';
import { useAuth } from './auth/AuthProvider';
import AdminPanel from './components/AdminPanel';
import ChatArea from './components/ChatArea';
import EvaluatePanel from './components/EvaluatePanel';
import FavoritesPanel from './components/FavoritesPanel';
import KolDetailView from './components/KolDetailView';
import KolRecommendPanel from './components/KolRecommendPanel';
import LoginPage from './components/LoginPage';
import MobileWorkspaceNav, { type WorkspacePane } from './components/MobileWorkspaceNav';
import RechargeModal from './components/RechargeModal';
import SessionList from './components/SessionList';
import TopPostsPanel from './components/TopPostsPanel';
import UniversalReport from './components/UniversalReport';
import { QUICK_TAB_IDS, WorkspaceTabs, type WorkspaceTab } from './components/WorkspaceTabs';
import { useWorkspace } from './hooks/useWorkspace';
import { isTerminalTaskStatus } from './state/taskEvents';
import type { QuickKolSelection } from './types';


export default function App() {
  const { user, status: authStatus, logout } = useAuth();
  const workspace = useWorkspace(authStatus === 'authenticated' ? user?.id : undefined);
  const [points, setPoints] = useState<number | null>(null);
  const [isRechargeOpen, setIsRechargeOpen] = useState(false);
  const [isAdminOpen, setIsAdminOpen] = useState(false);
  const [mobilePane, setMobilePane] = useState<WorkspacePane>('sessions');
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>('chat');
  const [favorites, setFavorites] = useState<readonly ApiFavorite[]>([]);
  const [quickView, setQuickView] = useState<QuickView | null>(null);
  const [selectedKol, setSelectedKol] = useState<QuickKolSelection | null>(null);

  const refreshWallet = useCallback(async () => {
    try {
      const wallet = await getWallet();
      setPoints(wallet.available);
    } catch {
      setPoints(null);
    }
  }, []);

  const syncFavorites = useCallback((items: readonly ApiFavorite[]) => {
    setFavorites(items);
  }, []);

  useEffect(() => {
    if (authStatus !== 'authenticated') {
      setFavorites([]);
      return;
    }
    let active = true;
    listFavorites().then(items => {
      if (active) syncFavorites(items);
    }).catch(() => {
      if (active) syncFavorites([]);
    });
    return () => { active = false; };
  }, [authStatus, syncFavorites]);

  useEffect(() => {
    if (authStatus !== 'authenticated') {
      setPoints(null);
      return;
    }

    void refreshWallet();
  }, [authStatus, refreshWallet]);

  useEffect(() => {
    if (authStatus !== 'authenticated') return;
    const status = workspace.taskRuntime?.status;
    const settled = workspace.taskRuntime?.activity === '本次调用积分已结算';
    if (!settled && !isTerminalTaskStatus(status)) return;
    void refreshWallet();
  }, [authStatus, refreshWallet, workspace.taskRuntime?.activity, workspace.taskRuntime?.status]);

  const handleCreateSession = async () => {
    setWorkspaceTab('chat');
    await workspace.createSession({});
    setMobilePane('chat');
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
    const changes: Record<string, unknown> = {
      brand,
      campaign_name: campaignName,
    };
    // brand/campaign 都为空时不 PATCH 空 title（后端会 422）。
    const title = campaignName ? `${brand}-${campaignName}` : brand;
    if (title.trim()) changes.title = title;
    await workspace.updateSession(id, changes);
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
    <div className="relative flex h-screen w-screen flex-col overflow-hidden bg-slate-100 antialiased text-slate-900 font-sans">
      <MobileWorkspaceNav active={mobilePane} onChange={setMobilePane} />

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <div className={`${mobilePane === 'sessions' ? 'block' : 'hidden'} h-full min-h-0 w-full shrink-0 xl:block xl:w-auto`}>
          <SessionList
            sessions={workspace.sessions}
            activeSessionId={workspace.activeSessionId ?? ''}
            onSelectSession={id => {
              setWorkspaceTab('chat');
              void workspace.selectSession(id);
              setMobilePane('chat');
            }}
            onCreateSession={() => void handleCreateSession().catch(() => undefined)}
            onToggleStar={id => void handleToggleStar(id).catch(() => undefined)}
            onRenameSession={(id, brand, campaignName) => void handleRenameSession(id, brand, campaignName).catch(() => undefined)}
            onDeleteSession={id => workspace.deleteSession(id)}
            user={user}
            onLogout={() => void logout()}
            points={points}
            onOpenRecharge={() => setIsRechargeOpen(true)}
            onOpenAdmin={user.role === 'admin' ? () => setIsAdminOpen(true) : undefined}
          />
        </div>

        <div className={`${mobilePane === 'chat' ? 'flex' : 'hidden'} h-full min-h-0 min-w-0 flex-1 flex-col xl:flex`}>
          {workspace.activeSession || QUICK_TAB_IDS.includes(workspaceTab) ? (
            <>
              <WorkspaceTabs
                active={workspaceTab}
                onChange={setWorkspaceTab}
                favoriteCount={favorites.length}
              />
              {workspaceTab === 'kol' && (
                <KolRecommendPanel
                  onBack={() => setWorkspaceTab('chat')}
                  onSelectKol={kol => setSelectedKol(kol)}
                />
              )}
              {workspaceTab === 'posts-xhs' && (
                <TopPostsPanel platform="xiaohongshu" onBack={() => setWorkspaceTab('chat')} />
              )}
              {workspaceTab === 'posts-dy' && (
                <TopPostsPanel platform="douyin" onBack={() => setWorkspaceTab('chat')} />
              )}
              {workspaceTab === 'evaluate' && (
                <EvaluatePanel onBack={() => setWorkspaceTab('chat')} />
              )}
              {workspace.activeSession && workspaceTab === 'chat' && (
                <ChatArea
                  session={workspace.activeSession}
                  onSendMessage={async text => {
                    await workspace.appendMessage(text);
                  }}
                  isAnalyzing={workspace.isAnalyzing}
                  isClarifying={workspace.isClarifying}
                  isMockMode={false}
                  flowNodes={workspace.taskRuntime?.nodes ?? []}
                  flowTerminal={isTerminalTaskStatus(workspace.taskRuntime?.status)}
                  flowTerminalLabel={workspace.taskRuntime?.phaseLabel}
                  assistantDraft={workspace.taskRuntime?.assistantDraft ?? ''}
                  onRetryMessage={messageId => workspace.retryMessage(messageId)}
                  followupStatus={workspace.activeSession.analysis?.followupStatus}
                  followupSuggestions={workspace.activeSession.analysis?.followupSuggestions}
                  followupError={typeof workspace.activeSession.analysis?.followupError?.message === 'string'
                    ? workspace.activeSession.analysis.followupError.message
                    : undefined}
                  onRetryFollowups={() => workspace.retryFollowups()}
                />
              )}
              {workspaceTab === 'favorites' && (
                <FavoritesPanel
                  refreshKey={0}
                  onFavoritesChange={syncFavorites}
                />
              )}
            </>
          ) : (
            <div className="flex flex-1 items-center justify-center bg-slate-50">
              <div className="text-center">
                <p className="text-xs font-medium text-slate-500">
                  {workspace.loading ? '正在加载历史会话…' : '请选择或新建一个 KOL 筛选会话'}
                </p>
                {!workspace.loading && (
                  <button
                    onClick={() => void handleCreateSession().catch(() => undefined)}
                    className="mt-3 rounded-lg bg-indigo-600 px-3 py-1.5 text-[11px] font-semibold text-white shadow-sm transition hover:bg-indigo-700"
                  >
                    新建分析会话
                  </button>
                )}
              </div>
            </div>
          )}
        </div>

        <div className={`${mobilePane === 'bi' ? 'block' : 'hidden'} h-full min-h-0 w-full shrink-0 xl:block xl:w-auto`}>
          {selectedKol ? (
            <KolDetailView selection={selectedKol} onClose={() => setSelectedKol(null)} />
          ) : (
            <UniversalReport
              report={workspace.activeSession?.analysisReport}
              taskStatus={(workspace.taskRuntime?.status ?? workspace.activeSession?.analysis?.status) as import('./api/contracts').ApiTaskStatus | undefined}
            />
          )}
        </div>
      </div>

      {workspace.error && (
        <div className="absolute bottom-5 left-1/2 z-40 -translate-x-1/2 rounded-xl border border-rose-100 bg-white px-4 py-2 text-xs font-medium text-rose-600 shadow-lg">
          {workspace.error}
        </div>
      )}

      <RechargeModal
        isOpen={isRechargeOpen}
        onClose={() => setIsRechargeOpen(false)}
        onRechargeSuccess={() => setIsRechargeOpen(false)}
        currentPoints={points}
        maxPoints={5000}
        isAvailable={false}
      />

      {user.role === 'admin' && (
        <AdminPanel
          isOpen={isAdminOpen}
          onClose={() => setIsAdminOpen(false)}
          currentUserId={user.id}
          currentUserNickname={user.nickname}
        />
      )}
    </div>
  );
}
