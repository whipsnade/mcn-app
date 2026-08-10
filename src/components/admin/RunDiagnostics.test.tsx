import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { getAdminRunDiagnostics, type AdminRunDiagnostics } from '../../api/adminGateway';
import RunDiagnostics from './RunDiagnostics';

vi.mock('../../api/adminGateway', () => ({
  getAdminRunDiagnostics: vi.fn(),
}));

const DIAGNOSTICS: AdminRunDiagnostics = {
  run: {
    id: 'run-1', tenant_id: 'tenant-a', session_id: 'sess-1', user_id: 'u-1',
    status: 'completed', outcome: 'success', runtime_backend: 'pi', error_code: null,
    created_at: '2026-08-09T10:00:00Z', started_at: '2026-08-09T10:00:01Z', completed_at: '2026-08-09T10:05:00Z',
  },
  attempts: [
    { id: 'att-1', attempt: 1, outcome: 'success', started_at: '2026-08-09T10:00:01Z', ended_at: '2026-08-09T10:05:00Z' },
  ],
  steps: [
    { id: 'step-1', sequence: 1, step_type: 'decision', status: 'completed', duration_ms: 1200, created_at: '2026-08-09T10:00:02Z' },
  ],
  tool_calls: [
    { id: 'call-1', logical_call_id: 'lc-1', service: 'datatap', internal_tool_name: 'search_kols', status: 'settled', points_reserved: 0, points_settled: 10, error_type: null, completed_at: '2026-08-09T10:00:03Z' },
    { id: 'call-2', logical_call_id: 'lc-2', service: 'datatap', internal_tool_name: 'get_kol_detail', status: 'unknown', points_reserved: 10, points_settled: null, error_type: null, completed_at: '2026-08-09T10:00:04Z' },
  ],
  events: [
    { id: 'evt-1', sequence: 1, event_type: 'run.started', created_at: '2026-08-09T10:00:01Z' },
  ],
  usage: [
    { id: 'use-1', kind: 'llm', backend: 'pi', provider: 'tencent', model: 'deepseek-v4-pro', input_tokens: 100, output_tokens: 200, cost_micros: 3000, currency: 'CNY', usage_status: 'observed', cost_status: 'priced', observed_at: '2026-08-09T10:00:05Z' },
  ],
  reconciliation: {
    run_id: 'run-1', tenant_id: 'tenant-a', reconciliation_status: 'mismatch',
    mismatch_codes: ['unknown_reserved_points'], mcp_settled_points: 10,
    run_reserved_points: 10, tenant_reserved_points: 10, unknown_reserved_points: 10,
  },
};

async function queryRun() {
  render(<RunDiagnostics />);
  fireEvent.change(screen.getByLabelText('Run ID'), { target: { value: 'run-1' } });
  fireEvent.click(screen.getByRole('button', { name: '查询' }));
  await waitFor(() => {
    expect(getAdminRunDiagnostics).toHaveBeenCalledWith('run-1');
  });
}

describe('RunDiagnostics', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getAdminRunDiagnostics).mockResolvedValue(DIAGNOSTICS);
  });

  it('does not query with a blank run id', () => {
    render(<RunDiagnostics />);
    const submit = screen.getByRole('button', { name: '查询' });
    expect(submit).toBeDisabled();
    fireEvent.click(submit);
    expect(getAdminRunDiagnostics).not.toHaveBeenCalled();
  });

  it('renders the run overview after a successful query', async () => {
    await queryRun();
    expect((await screen.findAllByText('run-1')).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/completed/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Backend pi/)).toBeTruthy();
  });

  it('renders attempts, steps, tool calls, events and usage as a timeline', async () => {
    await queryRun();
    const timeline = await screen.findByRole('list', { name: 'Run 时间线' });
    expect(timeline.textContent).toContain('尝试 #1');
    expect(timeline.textContent).toContain('decision');
    expect(timeline.textContent).toContain('search_kols');
    expect(timeline.textContent).toContain('run.started');
    expect(timeline.textContent).toContain('deepseek-v4-pro');
  });

  it('flags unknown tool calls and reserved points with textual badges', async () => {
    await queryRun();
    const timeline = await screen.findByRole('list', { name: 'Run 时间线' });
    const flagged = within(timeline).getByText('get_kol_detail').closest('li');
    expect(flagged?.textContent).toContain('unknown');
    expect(flagged?.textContent).toContain('预留中');
    const settled = within(timeline).getByText('search_kols').closest('li');
    expect(settled?.textContent).not.toContain('预留中');
  });

  it('shows reconciliation summary as scalar fields', async () => {
    await queryRun();
    expect(await screen.findByText('对账结果')).toBeTruthy();
    expect(screen.getAllByText(/mismatch/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/unknown_reserved_points/).length).toBeGreaterThan(0);
  });

  it('shows backend error codes verbatim and offers no replay or mutation controls', async () => {
    vi.mocked(getAdminRunDiagnostics).mockRejectedValue(new Error('run_not_found'));
    await queryRun();
    expect(await screen.findByRole('alert')).toHaveTextContent('run_not_found');
    expect(screen.queryByRole('button', { name: /重放|重试|恢复/ })).toBeNull();
  });

  it('shows a loading state while querying', async () => {
    let resolve: (value: AdminRunDiagnostics) => void = () => undefined;
    vi.mocked(getAdminRunDiagnostics).mockReturnValue(new Promise(value => { resolve = value; }));
    render(<RunDiagnostics />);
    fireEvent.change(screen.getByLabelText('Run ID'), { target: { value: 'run-1' } });
    fireEvent.click(screen.getByRole('button', { name: '查询' }));
    expect(await screen.findByRole('status')).toBeTruthy();
    resolve(DIAGNOSTICS);
    expect((await screen.findAllByText('run-1')).length).toBeGreaterThan(0);
  });
});
