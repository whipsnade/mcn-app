import React, { useCallback, useEffect, useMemo, useState } from 'react';

import type { ApiAgentMessage } from './api/agent';
import type { ApiFavorite } from './api/contracts';
import { listFavorites } from './api/favorites';
import { useAuth } from './auth/AuthProvider';
import AdminPanel from './components/AdminPanel';
import ArtifactWorkspace from './components/artifacts/ArtifactWorkspace';
import ChatArea from './components/ChatArea';
import KolDetailArtifactDialog from './components/artifacts/KolDetailArtifactDialog';
import FavoritesPanel from './components/FavoritesPanel';
import LoginPage from './components/LoginPage';
import MobileWorkspaceNav, { type WorkspacePane } from './components/MobileWorkspaceNav';
import RechargeModal from './components/RechargeModal';
import SessionList from './components/SessionList';
import { WorkspaceTabs, type WorkspaceTab } from './components/WorkspaceTabs';
import { useAgentWorkspace, type AgentWorkspaceSession } from './hooks/useAgentWorkspace';
import { useKolDetailFlow } from './hooks/useKolDetailFlow';
import { isTerminalRunStatus, type RunRuntimeState } from './state/agentEvents';
import type { Message, QuickKolSelection, Session } from './types';


// 把 agent 会话消息（ApiAgentMessage）适配为 ChatArea/SessionList 期望的 Message。
// ask_user 澄清的 metadata（type=clarification + options）映射到 Message.clarify，
// 供 ChatArea 的 clarificationByRun 为 Run 卡渲染澄清问题与选项 chips（§13.1）；
// Run 终态 utility 建议（metadata.suggestions）映射到 Message.suggestions，
// 由 ChatArea 在该 assistant 消息下方渲染追问建议 chips。
function toChatMessage(message: ApiAgentMessage): Message {
  return {
    id: message.id,
    sender: message.role === 'assistant' ? 'ai' : message.role === 'user' ? 'user' : 'system',
    text: message.content,
    timestamp: new Date(message.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
    runId: message.run_id ?? undefined,
    clarify: message.metadata?.type === 'clarification'
      ? { options: message.metadata.options ?? [] }
      : undefined,
    suggestions: message.metadata?.suggestions?.length ? message.metadata.suggestions : undefined,
  };
}

// agent 会话没有旧会话的结构化品牌/渠道元数据，全部置空（标题为主展示）。
function toChatSession(source: AgentWorkspaceSession): Session {
  return {
    id: source.id,
    title: source.title,
    brand: '',
    campaignName: null,
    status: 'completed',
    platform: '',
    category: '',
    targetAudience: '',
    summary: source.messages.find(message => message.role === 'user')?.content ?? '',
    messages: source.messages.map(toChatMessage),
    isStarred: false,
    createdAt: source.createdAt,
    updatedAt: source.updatedAt,
  };
}

// 历史 Run 的终态冻结快照：当前无历史 Run 的 SSE 回放，先用会话内 Run 元数据
// 生成最简终态卡（无步骤/工具明细），避免历史消息下的执行卡永久停留在「加载中」。
// 后续任务可在此按 runId 挂载 useAgentRun 回放 SSE，以渲染完整步骤/工具/思考。
function buildRunHistory(session: AgentWorkspaceSession | undefined): Record<string, RunRuntimeState> {
  const history: Record<string, RunRuntimeState> = {};
  for (const run of session?.runs ?? []) {
    history[run.id] = {
      runId: run.id,
      lastEventId: 0,
      connection: 'closed',
      status: run.status as RunRuntimeState['status'],
      steps: [],
      toolCalls: [],
      thinking: '',
      hasThinking: false,
      drafts: [],
      messageCompleted: false,
      artifactsVersion: 0,
    };
  }
  return history;
}

export default function App() {
  const { user, status: authStatus, logout } = useAuth();
  const workspace = useAgentWorkspace(authStatus === 'authenticated' ? user?.id : undefined);
  const [isRechargeOpen, setIsRechargeOpen] = useState(false);
  const [isAdminOpen, setIsAdminOpen] = useState(false);
  const [mobilePane, setMobilePane] = useState<WorkspacePane>('sessions');
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>('chat');
  const [favorites, setFavorites] = useState<readonly ApiFavorite[]>([]);
  const [favoritesLoading, setFavoritesLoading] = useState(false);
  const [favoritesRefreshKey, setFavoritesRefreshKey] = useState(0);
  // 收藏点击：经新 kol-details API 打开达人详情弹窗（review Fix 1——旧
  // /sessions/{id}/kol-selection/detail* 路由已随 Task 24 删除）。需要活跃
  // agent 会话；无会话时快照仍在卡片展示，刷新入口提示「新建会话后刷新」。
  const [favoriteDetail, setFavoriteDetail] = useState<{
    sessionId: string;
    platform: string;
    kolUid: string;
  } | null>(null);
  const favoriteDetailFlow = useKolDetailFlow(workspace.createKolDetail);

  const chatSession = useMemo(
    () => (workspace.activeSession ? toChatSession(workspace.activeSession) : undefined),
    [workspace.activeSession],
  );
  const runHistory = useMemo(
    () => buildRunHistory(workspace.activeSession),
    [workspace.activeSession],
  );
  const runStatus = workspace.run?.status;
  // 执行中 / 审核中视为分析中，禁止发送；澄清等待（clarification_requested）需用户作答，不阻塞输入。
  const isAnalyzing = workspace.busy || Boolean(
    runStatus && runStatus !== 'clarification_requested' && !isTerminalRunStatus(runStatus),
  );

  const handleFavoriteSelect = useCallback((kol: QuickKolSelection) => {
    if (!workspace.activeSessionId) return;
    setFavoriteDetail({ sessionId: workspace.activeSessionId, platform: kol.platform, kolUid: kol.kw_uid });
    void favoriteDetailFlow.run(workspace.activeSessionId, kol.platform, kol.kw_uid);
  }, [workspace.activeSessionId, favoriteDetailFlow.run]);

  // 有活跃会话时走新 kol-details API（createKolDetail）刷新达人详情。
  const handleRefreshFavoriteDetail = useCallback((favorite: ApiFavorite) => {
    if (!workspace.activeSessionId || !favorite.kol_uid) return;
    return workspace.createKolDetail(workspace.activeSessionId, favorite.platform, favorite.kol_uid);
  }, [workspace.activeSessionId, workspace.createKolDetail]);

  // 收藏是用户级数据：登出/会话切换不重置，仅未登录与拉取失败时按空列表处理（不阻塞面板）。
  const refreshFavorites = useCallback(() => {
    setFavoritesRefreshKey(key => key + 1);
  }, []);

  useEffect(() => {
    if (authStatus !== 'authenticated') {
      setFavorites([]);
      setFavoritesLoading(false);
      return;
    }
    let active = true;
    setFavoritesLoading(true);
    listFavorites().then(items => {
      if (active) setFavorites(items);
    }).catch(() => {
      if (active) setFavorites([]);
    }).finally(() => {
      if (active) setFavoritesLoading(false);
    });
    return () => { active = false; };
  }, [authStatus, favoritesRefreshKey]);

  const handleCreateSession = async () => {
    setWorkspaceTab('chat');
    await workspace.createSession();
    setMobilePane('chat');
  };

  const handleRenameSession = async (id: string, brand: string, campaignName: string) => {
    // agent 会话重命名只落 title；brand/campaign 都为空时不 PATCH 空标题。
    const title = campaignName ? `${brand}-${campaignName}` : brand;
    if (!title.trim()) return;
    await workspace.renameSession(id, title);
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
            sessions={workspace.sessions.map(toChatSession)}
            activeSessionId={workspace.activeSessionId ?? ''}
            onSelectSession={id => {
              setWorkspaceTab('chat');
              void workspace.selectSession(id);
              setMobilePane('chat');
            }}
            onCreateSession={() => void handleCreateSession().catch(() => undefined)}
            onRenameSession={(id, brand, campaignName) => void handleRenameSession(id, brand, campaignName).catch(() => undefined)}
            onDeleteSession={id => workspace.deleteSession(id)}
            user={user}
            onLogout={() => void logout()}
            points={workspace.wallet?.available ?? null}
            onOpenRecharge={() => setIsRechargeOpen(true)}
            onOpenAdmin={user.role === 'admin' ? () => setIsAdminOpen(true) : undefined}
          />
        </div>

        <div className={`${mobilePane === 'chat' ? 'flex' : 'hidden'} h-full min-h-0 min-w-0 flex-1 flex-col xl:flex`}>
          <WorkspaceTabs
            active={workspaceTab}
            onChange={setWorkspaceTab}
            favoriteCount={favorites.length}
          />
          {chatSession && workspaceTab === 'chat' ? (
            // 注（代码审查 I3）：agent 运行时用 Run 稳定态后的建议承载进一步分析，
            // resume 取代旧的 message retry；追问建议走 assistant 消息 metadata.suggestions
            // （toChatMessage 映射为 Message.suggestions，由 ChatArea 渲染 chips），
            // 旧任务流的 followup props 与「再次执行」保持不接线。
            <ChatArea
              session={chatSession}
              onSendMessage={text => workspace.sendMessage(chatSession.id, text)}
              isAnalyzing={isAnalyzing}
              isMockMode={false}
              run={workspace.run}
              runHistory={runHistory}
              onResumeRun={() => workspace.resumeActiveRun()}
              onCancelRun={() => workspace.cancelActiveRun()}
            />
          ) : workspaceTab === 'favorites' ? (
            <FavoritesPanel
              favorites={favorites}
              loading={favoritesLoading}
              onRefresh={refreshFavorites}
              onSelectKol={handleFavoriteSelect}
              sessionId={workspace.activeSessionId}
              onRefreshDetail={handleRefreshFavoriteDetail}
            />
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
          <ArtifactWorkspace
            sessionId={workspace.activeSessionId}
            artifacts={workspace.artifacts}
            markArtifactSeen={(module, lastSeenSequence) => {
              void workspace.markArtifactSeen(module, lastSeenSequence).catch(() => undefined);
            }}
            createKolDetail={workspace.createKolDetail}
          />
        </div>
      </div>

      {workspace.error && (
        <div className="absolute bottom-5 left-1/2 z-40 -translate-x-1/2 rounded-xl border border-rose-100 bg-white px-4 py-2 text-xs font-medium text-rose-600 shadow-lg">
          {workspace.error}
        </div>
      )}

      {favoriteDetail && (
        <KolDetailArtifactDialog
          payload={favoriteDetailFlow.payload}
          error={favoriteDetailFlow.error}
          onRetry={() => void favoriteDetailFlow.run(
            favoriteDetail.sessionId,
            favoriteDetail.platform,
            favoriteDetail.kolUid,
          )}
          onClose={() => setFavoriteDetail(null)}
        />
      )}

      <RechargeModal
        isOpen={isRechargeOpen}
        onClose={() => setIsRechargeOpen(false)}
        onRechargeSuccess={() => setIsRechargeOpen(false)}
        currentPoints={workspace.wallet?.available ?? null}
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
