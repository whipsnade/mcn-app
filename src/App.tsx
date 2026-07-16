import React, { useCallback, useEffect, useMemo, useState } from 'react';

import type { ApiCandidate, ApiCandidatePage, ApiFavorite } from './api/contracts';
import { createFavorite, deleteFavorite, listFavorites } from './api/favorites';
import { getWallet } from './api/wallet';
import { useAuth } from './auth/AuthProvider';
import AdminPanel from './components/AdminPanel';
import BiReport from './components/BiReport';
import CandidateList from './components/CandidateList';
import ChatArea from './components/ChatArea';
import FavoritesPanel from './components/FavoritesPanel';
import LoginPage from './components/LoginPage';
import MobileWorkspaceNav, { type WorkspacePane } from './components/MobileWorkspaceNav';
import NewSessionModal, { type NewSessionData } from './components/NewSessionModal';
import RechargeModal from './components/RechargeModal';
import SessionList from './components/SessionList';
import { WorkspaceTabs, type WorkspaceTab } from './components/WorkspaceTabs';
import { useWorkspace } from './hooks/useWorkspace';
import { isTerminalTaskStatus } from './state/taskEvents';


export default function App() {
  const { user, status: authStatus, logout } = useAuth();
  const workspace = useWorkspace(authStatus === 'authenticated' ? user?.id : undefined);
  const [points, setPoints] = useState<number | null>(null);
  const [isNewModalOpen, setIsNewModalOpen] = useState(false);
  const [isRechargeOpen, setIsRechargeOpen] = useState(false);
  const [isAdminOpen, setIsAdminOpen] = useState(false);
  const [mobilePane, setMobilePane] = useState<WorkspacePane>('sessions');
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>('chat');
  const [favorites, setFavorites] = useState<readonly ApiFavorite[]>([]);
  const [favoriteRefreshKey, setFavoriteRefreshKey] = useState(0);

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

  const handleCreateSession = async (data: NewSessionData) => {
    await workspace.createSession({
      brand: data.brand,
      campaign_name: data.campaignName.trim() || null,
      platforms: data.platforms,
      category: data.category,
      target_audience: data.targetAudience,
      budget_min: data.budgetMin,
      budget_max: data.budgetMax,
      initial_query: data.initialQuery,
      filters: data.kolName ? { kol_name: data.kolName } : {},
    });
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
    await workspace.updateSession(id, {
      brand,
      campaign_name: campaignName,
      title: campaignName ? `${brand}-${campaignName}` : brand,
    });
  };

  const candidatePage = useMemo<ApiCandidatePage | undefined>(() => {
    const session = workspace.activeSession;
    if (!session?.candidates || session.analysis?.candidateVersion === undefined) return undefined;
    return {
      task_id: session.analysis.taskId,
      version: session.analysis.candidateVersion,
      total: session.candidates.length,
      items: session.candidates.map(candidate => ({
        id: candidate.id,
        kol_id: candidate.kolId,
        platform: candidate.platform,
        platform_account_id: candidate.platformAccountId,
        nickname: candidate.nickname ?? null,
        profile_url: candidate.profileUrl ?? null,
        rank: candidate.rank,
        total_score: candidate.totalScore,
        scores: candidate.scores,
        matched_conditions: candidate.matchedConditions,
        risks: candidate.risks,
        recommendation: candidate.recommendation,
        metrics: candidate.metrics,
      })),
    };
  }, [workspace.activeSession]);
  const favoriteKolIds = useMemo(() => new Set(favorites.map(favorite => favorite.kol_id)), [favorites]);

  const handleFavorite = async (candidate: ApiCandidate) => {
    if (favoriteKolIds.has(candidate.kol_id)) {
      await deleteFavorite(candidate.kol_id);
      syncFavorites(favorites.filter(favorite => favorite.kol_id !== candidate.kol_id));
    } else {
      const favorite = await createFavorite({
        kol_id: candidate.kol_id,
        source_task_id: workspace.activeSession?.analysis?.taskId,
      });
      syncFavorites([...favorites, favorite]);
    }
    setFavoriteRefreshKey(current => current + 1);
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
        <div className={`${mobilePane === 'sessions' ? 'block' : 'hidden'} h-full w-full shrink-0 xl:block xl:w-auto`}>
          <SessionList
            sessions={workspace.sessions}
            activeSessionId={workspace.activeSessionId ?? ''}
            onSelectSession={id => {
              void workspace.selectSession(id);
              setMobilePane('chat');
            }}
            onOpenNewModal={() => setIsNewModalOpen(true)}
            onToggleStar={id => void handleToggleStar(id).catch(() => undefined)}
            onRenameSession={(id, brand, campaignName) => void handleRenameSession(id, brand, campaignName).catch(() => undefined)}
            user={user}
            onLogout={() => void logout()}
            points={points}
            onOpenRecharge={() => setIsRechargeOpen(true)}
            onOpenAdmin={user.role === 'admin' ? () => setIsAdminOpen(true) : undefined}
          />
        </div>

        <div className={`${mobilePane === 'chat' ? 'flex' : 'hidden'} h-full min-w-0 flex-1 flex-col xl:flex`}>
          {workspace.activeSession ? (
            <>
              <WorkspaceTabs
                active={workspaceTab}
                onChange={setWorkspaceTab}
                candidateCount={candidatePage?.total ?? 0}
                favoriteCount={favorites.length}
              />
              {workspaceTab === 'chat' && (
                <ChatArea
                  session={workspace.activeSession}
                  onSendMessage={async text => {
                    await workspace.appendMessage(text);
                  }}
                  isAnalyzing={workspace.isAnalyzing}
                  isMockMode
                  taskActivity={workspace.taskRuntime?.activity}
                />
              )}
              {workspaceTab === 'candidates' && (
                <CandidateList
                  page={candidatePage}
                  favoriteKolIds={favoriteKolIds}
                  onFavorite={handleFavorite}
                />
              )}
              {workspaceTab === 'favorites' && (
                <FavoritesPanel
                  refreshKey={favoriteRefreshKey}
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
                    onClick={() => setIsNewModalOpen(true)}
                    className="mt-3 rounded-lg bg-indigo-600 px-3 py-1.5 text-[11px] font-semibold text-white shadow-sm transition hover:bg-indigo-700"
                  >
                    新建分析会话
                  </button>
                )}
              </div>
            </div>
          )}
        </div>

        <div className={`${mobilePane === 'bi' ? 'block' : 'hidden'} h-full w-full shrink-0 xl:block xl:w-auto`}>
          <BiReport
            report={workspace.activeSession?.biReport}
            candidateVersion={workspace.activeSession?.analysis?.candidateVersion}
            selectedCandidates={candidatePage?.items}
            selectedCandidateVersion={candidatePage?.version}
          />
        </div>
      </div>

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
        isAvailable={false}
      />

      {user.role === 'admin' && (
        <AdminPanel
          isOpen={isAdminOpen}
          readOnly
          onClose={() => setIsAdminOpen(false)}
          accounts={[]}
          onUpdateAccounts={() => undefined}
          currentUserNickname={user.nickname}
        />
      )}
    </div>
  );
}
