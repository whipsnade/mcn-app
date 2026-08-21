import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import App from './App';
import type { ApiFavorite } from './api/contracts';
import { listFavorites } from './api/favorites';
import { useAuth } from './auth/AuthProvider';
import { useAgentWorkspace } from './hooks/useAgentWorkspace';

vi.mock('./api/favorites', () => ({
  createFavoriteByKey: vi.fn(),
  deleteFavorite: vi.fn(),
  deleteFavoriteByKey: vi.fn(),
  listFavorites: vi.fn(),
}));

vi.mock('./auth/AuthProvider', () => ({
  useAuth: vi.fn(),
}));

vi.mock('./hooks/useAgentWorkspace', () => ({
  useAgentWorkspace: vi.fn(),
}));

// App 集成测试：真实渲染 WorkspaceTabs / SessionList / FavoritesPanel / ArtifactWorkspace，
// 其余重组件替身化，避免牵扯会话流、报告等无关模块。
const chatAreaPropsRef = vi.hoisted(() => ({ current: undefined as unknown }));
vi.mock('./components/ChatArea', () => ({
  default: (props: unknown) => {
    chatAreaPropsRef.current = props;
    return <div>会话区</div>;
  },
}));
vi.mock('./components/MobileWorkspaceNav', () => ({ default: () => null }));
vi.mock('./components/artifacts/KolDetailArtifactDialog', () => ({ default: () => null }));
vi.mock('./components/RechargeModal', () => ({ default: () => null }));
vi.mock('./components/AdminPanel', () => ({ default: () => null }));

const mockUseAuth = vi.mocked(useAuth);
const mockUseAgentWorkspace = vi.mocked(useAgentWorkspace);
const mockListFavorites = vi.mocked(listFavorites);

const mockCreateKolDetail = vi.fn();
const mockSelectSession = vi.fn();
const mockCreateSession = vi.fn();
const mockRenameSession = vi.fn();
const mockDeleteSession = vi.fn();
const mockSendMessage = vi.fn();
const mockCancelActiveRun = vi.fn();
const mockResumeActiveRun = vi.fn();
const mockMarkArtifactSeen = vi.fn();

const AGENT_SESSION = {
  id: 's1',
  title: '统一 Agent 会话',
  status: 'active',
  createdAt: '2026-08-01T10:00:00',
  updatedAt: '2026-08-01T10:00:00',
  runs: [],
  messages: [],
};

// 可变 agent 工作区：activeSession/activeSessionId 通过 ref 控制，配合 rerender 模拟会话有无。
const workspaceRef: {
  current: {
    sessions: typeof AGENT_SESSION[];
    activeSession: typeof AGENT_SESSION | null;
    activeSessionId: string | undefined;
  };
} = {
  current: {
    sessions: [AGENT_SESSION],
    activeSession: AGENT_SESSION,
    activeSessionId: 's1',
  },
};

function workspaceValue() {
  return {
    sessions: workspaceRef.current.sessions,
    activeSession: workspaceRef.current.activeSession,
    activeSessionId: workspaceRef.current.activeSessionId,
    activeRunId: undefined,
    run: undefined,
    runHistory: {},
    artifacts: [],
    wallet: undefined,
    loading: false,
    busy: false,
    isCancelling: false,
    error: undefined,
    reload: vi.fn(),
    selectSession: mockSelectSession,
    createSession: mockCreateSession,
    renameSession: mockRenameSession,
    deleteSession: mockDeleteSession,
    sendMessage: mockSendMessage,
    cancelActiveRun: mockCancelActiveRun,
    resumeActiveRun: mockResumeActiveRun,
    createKolDetail: mockCreateKolDetail,
    refreshWallet: vi.fn(),
    markArtifactSeen: mockMarkArtifactSeen,
  };
}

function favoriteFixture(overrides: Partial<ApiFavorite> = {}): ApiFavorite {
  return {
    id: 'fav-1',
    kol_id: null,
    platform: 'xiaohongshu',
    platform_account_id: null,
    kol_uid: 'uid-1',
    nickname: '达人小A',
    profile_url: 'https://www.xiaohongshu.com/user/profile/uid-1',
    snapshot: { followers: 120000, quoted_price_cny: 12000, city: '上海' },
    note: null,
    source_task_id: null,
    created_at: '2026-07-20T10:00:00Z',
    ...overrides,
  };
}

describe('App 集成：一次性切换到统一 Agent 工作区', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    workspaceRef.current = {
      sessions: [AGENT_SESSION],
      activeSession: AGENT_SESSION,
      activeSessionId: 's1',
    };
    mockUseAuth.mockReturnValue({
      user: { id: 'user-1', nickname: '测试用户', role: 'user' },
      status: 'authenticated',
      logout: vi.fn(),
    } as never);
    mockUseAgentWorkspace.mockImplementation(() => workspaceValue() as never);
    mockListFavorites.mockResolvedValue([]);
  });

  it('会话列表来自 agent 工作区，右侧渲染 ArtifactWorkspace，四个快捷入口消失', async () => {
    render(<App />);

    // 会话列表由 useAgentWorkspace 支撑，渲染 agent 会话
    expect(mockUseAgentWorkspace).toHaveBeenCalledWith('user-1');
    expect(screen.getByText('统一 Agent 会话')).toBeVisible();

    // 顶部只保留 智能会话 + 收藏
    expect(screen.getByRole('tab', { name: '智能会话' })).toBeVisible();
    expect(screen.getByRole('tab', { name: '已收藏 0' })).toBeVisible();

    // 四个快捷入口不再出现
    for (const name of ['达人推荐', '活动评估', '小红书爆贴', '抖音爆贴']) {
      expect(screen.queryByRole('tab', { name })).toBeNull();
      expect(screen.queryByText(name)).toBeNull();
    }

    // 右侧 BI 区渲染 ArtifactWorkspace（品牌分析/活动分析/达人）
    expect(screen.getByRole('tab', { name: '品牌分析' })).toBeVisible();
    expect(screen.getByRole('tab', { name: '活动分析' })).toBeVisible();
    expect(screen.getByRole('tab', { name: '达人' })).toBeVisible();
  });

  it('把 assistant 消息 metadata.suggestions 映射为 ChatArea 消息的 suggestions', () => {
    const sessionWithSuggestions = {
      ...AGENT_SESSION,
      messages: [{
        id: 'm-ai',
        role: 'assistant',
        content: '分析完成',
        sequence: 1,
        run_id: 'run-1',
        created_at: '2026-08-01T10:00:01',
        metadata: { suggestions: ['对比一下竞品的投放节奏'] },
      }],
    };
    workspaceRef.current = {
      sessions: [sessionWithSuggestions],
      activeSession: sessionWithSuggestions,
      activeSessionId: 's1',
    };

    render(<App />);

    const props = chatAreaPropsRef.current as {
      session: { messages: { sender: string; suggestions?: string[] }[] };
    };
    expect(props.session.messages[0].sender).toBe('ai');
    expect(props.session.messages[0].suggestions).toEqual(['对比一下竞品的投放节奏']);
  });

  it('把 workspace 的历史 Run 回放结果透传给 ChatArea 的 runHistory', () => {
    const replayed = {
      'run-1': {
        runId: 'run-1',
        lastEventId: 7,
        connection: 'closed',
        status: 'completed',
        steps: [{ id: 'run', label: '开始执行', status: 'succeeded' }],
        toolCalls: [{ id: 'tool-5', name: 'brand_search', status: 'succeeded' }],
        thinking: '正在检索品牌声量',
        hasThinking: true,
        thinkingStatus: 'completed',
        drafts: [],
        messageCompleted: true,
        artifactsVersion: 0,
      },
    };
    workspaceRef.current = {
      sessions: [AGENT_SESSION],
      activeSession: AGENT_SESSION,
      activeSessionId: 's1',
    };
    mockUseAgentWorkspace.mockImplementation(() => ({
      ...workspaceValue(),
      runHistory: replayed,
    }) as never);

    render(<App />);

    const props = chatAreaPropsRef.current as { runHistory: Record<string, { toolCalls: unknown[] }> };
    expect(props.runHistory['run-1'].toolCalls).toHaveLength(1);
  });

  it('收藏保留：无会话时展示保存的快照与「新建会话后刷新」，不回退旧 Quick API', async () => {
    workspaceRef.current = {
      sessions: [AGENT_SESSION],
      activeSession: null,
      activeSessionId: undefined,
    };
    mockListFavorites.mockResolvedValue([favoriteFixture()]);

    render(<App />);
    fireEvent.click(screen.getByRole('tab', { name: /已收藏/ }));

    // 保存的快照优先展示
    expect(await screen.findByText('达人小A')).toBeVisible();
    expect(screen.getByText(/粉丝 12万/)).toBeVisible();
    expect(screen.getByText(/¥12,000/)).toBeVisible();

    // 无会话：刷新入口提示「新建会话后刷新」
    expect(screen.getByText('新建会话后刷新')).toBeVisible();

    // 点击达人详情：无活跃会话不触发新 kol-details（也不回退旧 Quick/旧 selection detail API）
    fireEvent.click(screen.getByRole('button', { name: /查看达人详情 达人小A/ }));
    await act(async () => {});
    expect(mockCreateKolDetail).not.toHaveBeenCalled();
  });

  it('收藏保留：有活跃会话时打开达人详情走新 kol-details API，而非旧 selection detail 路由', async () => {
    workspaceRef.current = {
      sessions: [AGENT_SESSION],
      activeSession: AGENT_SESSION,
      activeSessionId: 's1',
    };
    mockListFavorites.mockResolvedValue([favoriteFixture()]);
    mockCreateKolDetail.mockResolvedValue({
      run_id: null,
      artifact_id: null,
      cached: true,
      detail: {
        schema_version: 'kol_detail_v2',
        module: 'kol',
        scope: { platform: 'xiaohongshu', kol_uid: 'uid-1', selection_artifact_id: null, selection_version: null },
        data: { cache: { hit: true, fetched_at: '', expires_at: '' } },
        narrative: { profile_summary: '', content_strengths: [], commercial_notes: [], risk_notes: [] },
      },
    });

    render(<App />);
    fireEvent.click(screen.getByRole('tab', { name: /已收藏/ }));
    expect(await screen.findByText('达人小A')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: /查看达人详情 达人小A/ }));
    await waitFor(() => {
      expect(mockCreateKolDetail).toHaveBeenCalledWith('s1', 'xiaohongshu', 'uid-1');
    });
  });

  it('收藏保留：有活跃会话时经新 kol-details API 刷新', async () => {
    workspaceRef.current = {
      sessions: [AGENT_SESSION],
      activeSession: AGENT_SESSION,
      activeSessionId: 's1',
    };
    mockListFavorites.mockResolvedValue([favoriteFixture()]);

    render(<App />);
    fireEvent.click(screen.getByRole('tab', { name: /已收藏/ }));

    expect(await screen.findByText('达人小A')).toBeVisible();

    fireEvent.click(screen.getByRole('button', { name: /刷新达人详情/ }));
    await waitFor(() => {
      expect(mockCreateKolDetail).toHaveBeenCalledWith('s1', 'xiaohongshu', 'uid-1');
    });
  });
});
