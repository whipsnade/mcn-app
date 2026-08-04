import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiAgentMessage, ApiAgentRun, ApiAgentSession, ApiAgentSessionDetail } from '../api/agent';
import * as agentApi from '../api/agent';
import type { ApiAgentArtifact } from '../api/agentArtifacts';
import * as agentArtifactsApi from '../api/agentArtifacts';
import type { ApiWallet } from '../api/contracts';
import * as walletApi from '../api/wallet';
import { initialRunRuntime } from '../state/agentEvents';
import { useAgentRun } from './useAgentRun';
import { useAgentWorkspace } from './useAgentWorkspace';

vi.mock('../api/agent', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/agent')>();
  return {
    ...actual,
    createSession: vi.fn(),
    listSessions: vi.fn(),
    getSession: vi.fn(),
    patchSession: vi.fn(),
    deleteSession: vi.fn(),
    sendMessage: vi.fn(),
    cancelRun: vi.fn(),
    resumeRun: vi.fn(),
    createKolDetailRun: vi.fn(),
  };
});

vi.mock('../api/agentArtifacts', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/agentArtifacts')>();
  return {
    ...actual,
    listArtifacts: vi.fn(),
    getArtifact: vi.fn(),
    getArtifactVersion: vi.fn(),
    markArtifactRead: vi.fn(),
    exportArtifact: vi.fn(),
  };
});

vi.mock('../api/wallet', () => ({ getWallet: vi.fn() }));

vi.mock('./useAgentRun', async importOriginal => {
  const actual = await importOriginal<typeof import('./useAgentRun')>();
  return { ...actual, useAgentRun: vi.fn() };
});

const s1: ApiAgentSession = {
  id: 's1',
  title: '会话一',
  status: 'active',
  created_at: '2026-08-01T10:00:00',
  updated_at: '2026-08-01T10:00:00',
};
const s2: ApiAgentSession = {
  id: 's2',
  title: '会话二',
  status: 'active',
  created_at: '2026-08-01T11:00:00',
  updated_at: '2026-08-01T11:00:00',
};

const run1: ApiAgentRun = {
  id: 'run-1',
  session_id: 's1',
  parent_run_id: null,
  profile_name: 'session_analyst_v1',
  status: 'running',
  outcome: null,
  decision_count: 1,
  review_count: 0,
  revision_count: 0,
  error_code: null,
  started_at: '2026-08-01T10:00:00',
  paused_at: null,
  completed_at: null,
};

const s1Detail: ApiAgentSessionDetail = { ...s1, messages: [], runs: [run1] };
const s2Detail: ApiAgentSessionDetail = { ...s2, messages: [], runs: [] };

function makeRun(id: string, status: string): ApiAgentRun {
  return { ...run1, id, status };
}

const artifact: ApiAgentArtifact = {
  id: 'art-1',
  module: 'brand',
  artifact_type: 'brand_report_v3',
  parent_artifact_id: null,
  artifact_key: 'brand_report',
  status: 'published',
  latest_version: 3,
  activity_sequence: 9,
  created_at: '2026-08-01T10:00:00',
  updated_at: '2026-08-01T10:00:00',
};

const wallet: ApiWallet = { balance: 100, reserved: 10, available: 90 };

describe('useAgentWorkspace', () => {
  beforeEach(() => {
    vi.mocked(walletApi.getWallet).mockResolvedValue(wallet);
    vi.mocked(agentArtifactsApi.listArtifacts).mockResolvedValue([]);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('loads the session list, selects the first, loads its artifacts and wallet, and subscribes to its run', async () => {
    vi.mocked(agentApi.listSessions).mockResolvedValue([s1, s2]);
    vi.mocked(agentApi.getSession).mockImplementation(async id => (id === 's1' ? s1Detail : s2Detail));
    vi.mocked(agentArtifactsApi.listArtifacts).mockResolvedValue([artifact]);

    const { result } = renderHook(() => useAgentWorkspace('user-1'));

    await waitFor(() => expect(result.current.activeSessionId).toBe('s1'));

    expect(result.current.sessions).toHaveLength(2);
    expect(result.current.sessions[0]).toMatchObject({ id: 's1', title: '会话一', runs: [run1] });
    expect(agentApi.getSession).toHaveBeenCalledWith('s1');
    expect(agentArtifactsApi.listArtifacts).toHaveBeenCalledWith('s1');
    expect(walletApi.getWallet).toHaveBeenCalled();
    expect(result.current.wallet).toEqual(wallet);
    expect(vi.mocked(useAgentRun).mock.calls.at(-1)?.[0]).toBe('run-1');
  });

  it('anchors to the active run over a later terminal run in server order', async () => {
    // §6.4：恢复锚点优先活动 Run（queued/running/reviewing/paused）；
    // 即使服务端顺序里更后有终态 Run（如 kol_detail 辅助 Run）也不锚定它。
    const detail: ApiAgentSessionDetail = {
      ...s1,
      messages: [],
      runs: [
        makeRun('run-active', 'running'),
        makeRun('run-detail', 'completed'),
      ],
    };
    vi.mocked(agentApi.listSessions).mockResolvedValue([s1]);
    vi.mocked(agentApi.getSession).mockResolvedValue(detail);

    const { result } = renderHook(() => useAgentWorkspace('user-1'));
    await waitFor(() => expect(result.current.activeSessionId).toBe('s1'));

    expect(result.current.activeRunId).toBe('run-active');
  });

  it('anchors to the latest active run when several runs are active', async () => {
    const detail: ApiAgentSessionDetail = {
      ...s1,
      messages: [],
      runs: [
        makeRun('run-queued', 'queued'),
        makeRun('run-paused', 'paused'),
        makeRun('run-done', 'completed'),
      ],
    };
    vi.mocked(agentApi.listSessions).mockResolvedValue([s1]);
    vi.mocked(agentApi.getSession).mockResolvedValue(detail);

    const { result } = renderHook(() => useAgentWorkspace('user-1'));
    await waitFor(() => expect(result.current.activeSessionId).toBe('s1'));

    expect(result.current.activeRunId).toBe('run-paused');
  });

  it('anchors to the last run in server order when no run is active', async () => {
    // 全部终态：取服务端顺序（created_at 升序）的最后一个，与列表顺序无关的
    // 随机 uuid 不再影响锚点。
    const detail: ApiAgentSessionDetail = {
      ...s1,
      messages: [],
      runs: [
        makeRun('run-first', 'completed'),
        makeRun('run-latest', 'failed'),
      ],
    };
    vi.mocked(agentApi.listSessions).mockResolvedValue([s1]);
    vi.mocked(agentApi.getSession).mockResolvedValue(detail);

    const { result } = renderHook(() => useAgentWorkspace('user-1'));
    await waitFor(() => expect(result.current.activeSessionId).toBe('s1'));

    expect(result.current.activeRunId).toBe('run-latest');
  });

  it('does not load when no user is present', () => {
    renderHook(() => useAgentWorkspace());

    expect(agentApi.listSessions).not.toHaveBeenCalled();
    expect(walletApi.getWallet).not.toHaveBeenCalled();
  });

  it('creates a session and makes it active', async () => {
    vi.mocked(agentApi.listSessions).mockResolvedValue([]);
    const newSession: ApiAgentSession = {
      id: 's3',
      title: '新会话1',
      status: 'active',
      created_at: '2026-08-01T12:00:00',
      updated_at: '2026-08-01T12:00:00',
    };
    vi.mocked(agentApi.createSession).mockResolvedValue(newSession);

    const { result } = renderHook(() => useAgentWorkspace('user-1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    let createdId: string | undefined;
    await act(async () => {
      createdId = (await result.current.createSession('新会话1'))?.id;
    });

    expect(agentApi.createSession).toHaveBeenCalledWith('新会话1');
    expect(createdId).toBe('s3');
    expect(result.current.activeSessionId).toBe('s3');
    expect(result.current.sessions[0]).toMatchObject({ id: 's3', title: '新会话1' });
  });

  it('selects a session, reloads artifacts and switches the run subscription', async () => {
    vi.mocked(agentApi.listSessions).mockResolvedValue([s1, s2]);
    vi.mocked(agentApi.getSession).mockImplementation(async id => (id === 's1' ? s1Detail : s2Detail));

    const { result } = renderHook(() => useAgentWorkspace('user-1'));
    await waitFor(() => expect(result.current.activeSessionId).toBe('s1'));

    await act(async () => {
      await result.current.selectSession('s2');
    });

    expect(agentApi.getSession).toHaveBeenCalledWith('s2');
    expect(result.current.activeSessionId).toBe('s2');
    expect(result.current.activeRunId).toBeUndefined();
    expect(agentArtifactsApi.listArtifacts).toHaveBeenCalledWith('s2');
    expect(vi.mocked(useAgentRun).mock.calls.at(-1)?.[0]).toBeUndefined();
  });

  it('renames a session', async () => {
    vi.mocked(agentApi.listSessions).mockResolvedValue([s1]);
    vi.mocked(agentApi.getSession).mockResolvedValue(s1Detail);
    vi.mocked(agentApi.patchSession).mockResolvedValue({ ...s1, title: '新标题' });

    const { result } = renderHook(() => useAgentWorkspace('user-1'));
    await waitFor(() => expect(result.current.activeSessionId).toBe('s1'));

    await act(async () => {
      await result.current.renameSession('s1', '新标题');
    });

    expect(agentApi.patchSession).toHaveBeenCalledWith('s1', { title: '新标题' });
    expect(result.current.sessions[0].title).toBe('新标题');
  });

  it('deletes the active session and selects the next one', async () => {
    vi.mocked(agentApi.listSessions).mockResolvedValue([s1, s2]);
    vi.mocked(agentApi.getSession).mockImplementation(async id => (id === 's1' ? s1Detail : s2Detail));
    vi.mocked(agentApi.deleteSession).mockResolvedValue(undefined);

    const { result } = renderHook(() => useAgentWorkspace('user-1'));
    await waitFor(() => expect(result.current.activeSessionId).toBe('s1'));

    await act(async () => {
      await result.current.deleteSession('s1');
    });

    expect(agentApi.deleteSession).toHaveBeenCalledWith('s1');
    expect(result.current.sessions.map(session => session.id)).toEqual(['s2']);
    expect(result.current.activeSessionId).toBe('s2');
  });

  it('refreshes the wallet balance', async () => {
    vi.mocked(agentApi.listSessions).mockResolvedValue([]);
    vi.mocked(walletApi.getWallet)
      .mockResolvedValueOnce(wallet)
      .mockResolvedValueOnce({ balance: 80, reserved: 0, available: 80 });

    const { result } = renderHook(() => useAgentWorkspace('user-1'));
    await waitFor(() => expect(result.current.wallet).toEqual(wallet));

    await act(async () => {
      await result.current.refreshWallet();
    });

    expect(result.current.wallet).toEqual({ balance: 80, reserved: 0, available: 80 });
  });

  it('sends a message and subscribes to the returned run', async () => {
    vi.mocked(agentApi.listSessions).mockResolvedValue([s1]);
    vi.mocked(agentApi.getSession).mockResolvedValue(s1Detail);
    vi.mocked(agentApi.sendMessage).mockResolvedValue({
      run_id: 'run-2',
      session_id: 's1',
      message_id: 'm2',
      status: 'queued',
      reused: false,
    });

    const { result } = renderHook(() => useAgentWorkspace('user-1'));
    await waitFor(() => expect(result.current.activeSessionId).toBe('s1'));

    let runId: string | undefined;
    await act(async () => {
      runId = await result.current.sendMessage('s1', '帮我分析品牌');
    });

    expect(agentApi.sendMessage).toHaveBeenCalledWith('s1', '帮我分析品牌');
    expect(runId).toBe('run-2');
    expect(result.current.activeRunId).toBe('run-2');
    expect(vi.mocked(useAgentRun).mock.calls.at(-1)?.[0]).toBe('run-2');
  });

  it('optimistically shows the user message and refetches the session detail after the run settles', async () => {
    const userMessage: ApiAgentMessage = {
      id: 'm-user',
      role: 'user',
      content: '帮我分析品牌',
      sequence: 1,
      run_id: 'run-2',
      created_at: '2026-08-02T10:00:00',
    };
    const assistantReply: ApiAgentMessage = {
      id: 'm-ai',
      role: 'assistant',
      content: '已完成分析',
      sequence: 2,
      run_id: 'run-2',
      created_at: '2026-08-02T10:00:01',
    };
    const settledDetail: ApiAgentSessionDetail = {
      ...s1,
      messages: [userMessage, assistantReply],
      runs: [run1, { ...run1, id: 'run-2', status: 'completed', completed_at: '2026-08-02T10:00:01' }],
    };
    vi.mocked(agentApi.listSessions).mockResolvedValue([s1]);
    // 初始加载返回无消息详情；Run 稳定态后的回拉返回含 assistant 回复的详情。
    vi.mocked(agentApi.getSession)
      .mockResolvedValueOnce(s1Detail)
      .mockResolvedValue(settledDetail);
    vi.mocked(agentApi.sendMessage).mockResolvedValue({
      run_id: 'run-2',
      session_id: 's1',
      message_id: 'm2',
      status: 'queued',
      reused: false,
    });

    const { rerender, result } = renderHook(() => useAgentWorkspace('user-1'));
    await waitFor(() => expect(result.current.activeSessionId).toBe('s1'));

    // 发送后用户消息立即出现（乐观插入），并回填 run_id 供执行卡锚定。
    await act(async () => {
      await result.current.sendMessage('s1', '帮我分析品牌');
    });
    expect(result.current.activeRunId).toBe('run-2');
    const optimistic = result.current.activeSession?.messages.find(message => message.content === '帮我分析品牌');
    expect(optimistic).toBeTruthy();
    expect(optimistic?.run_id).toBe('run-2');
    expect(result.current.activeSession?.messages.some(message => message.content === '已完成分析')).toBe(false);

    // Run 到达终态 → 回拉会话详情，assistant 回复出现在消息流。
    vi.mocked(useAgentRun).mockReturnValue({
      ...initialRunRuntime('run-2'),
      status: 'completed',
      connection: 'closed',
      steps: [{ id: 'terminal-1', label: '分析完成', status: 'succeeded' }],
    });
    await act(async () => {
      rerender();
    });
    await waitFor(() => {
      expect(result.current.activeSession?.messages.some(message => message.content === '已完成分析')).toBe(true);
    });
    expect(agentApi.getSession).toHaveBeenCalledTimes(2);
  });

  it('surfaces utility suggestions from the settled-run refetch without an extra delayed retry', async () => {
    const assistantReply: ApiAgentMessage = {
      id: 'm-ai',
      role: 'assistant',
      content: '已完成分析',
      sequence: 2,
      run_id: 'run-2',
      created_at: '2026-08-02T10:00:01',
      metadata: { suggestions: ['对比一下竞品的投放节奏'] },
    };
    const settledDetail: ApiAgentSessionDetail = {
      ...s1,
      messages: [assistantReply],
      runs: [run1, { ...run1, id: 'run-2', status: 'completed', completed_at: '2026-08-02T10:00:01' }],
    };
    vi.mocked(agentApi.listSessions).mockResolvedValue([s1]);
    vi.mocked(agentApi.getSession)
      .mockResolvedValueOnce(s1Detail)
      .mockResolvedValue(settledDetail);
    vi.mocked(agentApi.sendMessage).mockResolvedValue({
      run_id: 'run-2',
      session_id: 's1',
      message_id: 'm2',
      status: 'queued',
      reused: false,
    });
    // clearAllMocks 不清实现：显式复位，避免上一条测试的 useAgentRun 返回值
    // 让稳定态 effect 在初始加载时提前触发并被去重键拦截。
    vi.mocked(useAgentRun).mockReturnValue(undefined);

    const { rerender, result } = renderHook(() => useAgentWorkspace('user-1'));
    await waitFor(() => expect(result.current.activeSessionId).toBe('s1'));

    await act(async () => {
      await result.current.sendMessage('s1', '帮我分析品牌');
    });
    vi.mocked(useAgentRun).mockReturnValue({
      ...initialRunRuntime('run-2'),
      status: 'completed',
      connection: 'closed',
    });
    await act(async () => {
      rerender();
    });

    // 稳定态回拉带上 suggestions：消息流直接可见，无需延迟补拉。
    await waitFor(() => {
      expect(result.current.activeSession?.messages[0]?.metadata?.suggestions)
        .toEqual(['对比一下竞品的投放节奏']);
    });
    expect(agentApi.getSession).toHaveBeenCalledTimes(2);
  });

  it('refetches once more after a delay when utility suggestions land after the terminal settle', async () => {
    // utility 建议在 settle 后异步落库：终态即时回拉通常早于 suggestions 写入，
    // hook 补一次延迟回拉把建议带入消息流。
    const assistantReply: ApiAgentMessage = {
      id: 'm-ai',
      role: 'assistant',
      content: '已完成分析',
      sequence: 2,
      run_id: 'run-2',
      created_at: '2026-08-02T10:00:01',
    };
    const settledWithout: ApiAgentSessionDetail = {
      ...s1,
      messages: [assistantReply],
      runs: [run1, { ...run1, id: 'run-2', status: 'completed', completed_at: '2026-08-02T10:00:01' }],
    };
    const settledWith: ApiAgentSessionDetail = {
      ...settledWithout,
      messages: [{ ...assistantReply, metadata: { suggestions: ['按预算重新排序达人名单'] } }],
    };
    vi.mocked(agentApi.listSessions).mockResolvedValue([s1]);
    vi.mocked(agentApi.getSession)
      .mockResolvedValueOnce(s1Detail)
      .mockResolvedValueOnce(settledWithout)
      .mockResolvedValue(settledWith);
    vi.mocked(agentApi.sendMessage).mockResolvedValue({
      run_id: 'run-2',
      session_id: 's1',
      message_id: 'm2',
      status: 'queued',
      reused: false,
    });
    // clearAllMocks 不清实现：显式复位，避免上一条测试的 useAgentRun 返回值
    // 让稳定态 effect 在初始加载时提前触发并被去重键拦截。
    vi.mocked(useAgentRun).mockReturnValue(undefined);

    const { rerender, result } = renderHook(() => useAgentWorkspace('user-1'));
    await waitFor(() => expect(result.current.activeSessionId).toBe('s1'));
    await act(async () => {
      await result.current.sendMessage('s1', '帮我分析品牌');
    });

    vi.useFakeTimers();
    try {
      vi.mocked(useAgentRun).mockReturnValue({
        ...initialRunRuntime('run-2'),
        status: 'completed',
        connection: 'closed',
      });
      await act(async () => {
        rerender();
      });
      // 即时回拉已完成，但建议尚未落库。
      expect(agentApi.getSession).toHaveBeenCalledTimes(2);
      expect(result.current.activeSession?.messages[0]?.metadata?.suggestions).toBeUndefined();

      await act(async () => {
        vi.advanceTimersByTime(5_000);
      });
      expect(agentApi.getSession).toHaveBeenCalledTimes(3);
      expect(result.current.activeSession?.messages[0]?.metadata?.suggestions)
        .toEqual(['按预算重新排序达人名单']);
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps suggestions scoped to their own session when switching sessions', async () => {
    const s1Assistant: ApiAgentMessage = {
      id: 'm-ai-1',
      role: 'assistant',
      content: '会话一分析完成',
      sequence: 1,
      run_id: 'run-1',
      created_at: '2026-08-01T10:00:01',
      metadata: { suggestions: ['会话一的追问'] },
    };
    const s2Assistant: ApiAgentMessage = {
      id: 'm-ai-2',
      role: 'assistant',
      content: '会话二分析完成',
      sequence: 1,
      run_id: null,
      created_at: '2026-08-01T11:00:01',
      metadata: { suggestions: ['会话二的追问'] },
    };
    vi.mocked(agentApi.listSessions).mockResolvedValue([s1, s2]);
    vi.mocked(agentApi.getSession).mockImplementation(async id => (
      id === 's1'
        ? { ...s1, messages: [s1Assistant], runs: [run1] }
        : { ...s2, messages: [s2Assistant], runs: [] }
    ));

    const { result } = renderHook(() => useAgentWorkspace('user-1'));
    await waitFor(() => expect(result.current.activeSessionId).toBe('s1'));
    expect(result.current.activeSession?.messages[0]?.metadata?.suggestions).toEqual(['会话一的追问']);

    await act(async () => {
      await result.current.selectSession('s2');
    });

    // 切换后建议跟随新会话；旧会话仍保留自己的建议。
    expect(result.current.activeSession?.messages[0]?.metadata?.suggestions).toEqual(['会话二的追问']);
    expect(result.current.sessions.find(session => session.id === 's1')?.messages[0]?.metadata?.suggestions)
      .toEqual(['会话一的追问']);
  });

  it('refetches artifacts only when artifact-relevant run events arrive, not on thinking deltas', async () => {
    vi.mocked(agentApi.listSessions).mockResolvedValue([s1]);
    vi.mocked(agentApi.getSession).mockResolvedValue(s1Detail);
    vi.mocked(agentArtifactsApi.listArtifacts).mockResolvedValue([]);
    const base = {
      ...initialRunRuntime('run-1'),
      connection: 'connected' as const,
      lastEventId: 1,
      artifactsVersion: 0,
    };
    vi.mocked(useAgentRun).mockReturnValue(base);

    const { rerender } = renderHook(() => useAgentWorkspace('user-1'));
    await waitFor(() => expect(agentArtifactsApi.listArtifacts).toHaveBeenCalledTimes(1));

    // 纯 thinking 增量：lastEventId 增长但 artifactsVersion 不变 → 不重拉目录。
    vi.mocked(useAgentRun).mockReturnValue({
      ...base,
      lastEventId: 99,
      thinking: '思考增量',
      hasThinking: true,
    });
    await act(async () => {
      rerender();
    });
    expect(agentArtifactsApi.listArtifacts).toHaveBeenCalledTimes(1);

    // artifact.published 事件：artifactsVersion 增长 → 重拉目录。
    vi.mocked(useAgentRun).mockReturnValue({
      ...base,
      lastEventId: 100,
      artifactsVersion: 1,
      drafts: [{ artifactId: 'art-1', module: 'brand', version: 1, status: 'published' }],
    });
    await act(async () => {
      rerender();
    });
    expect(agentArtifactsApi.listArtifacts).toHaveBeenCalledTimes(2);
  });
});
