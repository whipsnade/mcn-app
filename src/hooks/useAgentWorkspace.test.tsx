import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiAgentRun, ApiAgentSession, ApiAgentSessionDetail } from '../api/agent';
import * as agentApi from '../api/agent';
import type { ApiAgentArtifact } from '../api/agentArtifacts';
import * as agentArtifactsApi from '../api/agentArtifacts';
import type { ApiWallet } from '../api/contracts';
import * as walletApi from '../api/wallet';
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
});
