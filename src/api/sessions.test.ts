import { beforeEach, describe, expect, it, vi } from 'vitest';

import { request } from './client';
import { deleteSession, toSession } from './sessions';


vi.mock('./client', () => ({ request: vi.fn() }));


describe('toSession', () => {
  beforeEach(() => {
    vi.mocked(request).mockReset();
  });

  it('maps server history without MCN fields', () => {
    const session = toSession({
      id: 's-1',
      title: '示例品牌-夏季选人',
      brand: '示例品牌',
      campaign_name: '夏季选人',
      status: 'draft',
      platforms: ['xiaohongshu'],
      category: '美妆护肤',
      target_audience: '18-30 岁女性',
      budget_min: '30000.00',
      budget_max: '80000.00',
      filters: {},
      is_starred: false,
      messages: [{
        id: 'm-1',
        role: 'user',
        content: '寻找达人',
        sequence: 1,
        metadata: {},
        created_at: '2026-07-14T10:00:00Z',
      }],
      created_at: '2026-07-14T10:00:00Z',
      updated_at: '2026-07-14T10:00:00Z',
    });

    expect(session.platform).toBe('Xiaohongshu');
    expect(session.messages[0].text).toBe('寻找达人');
    expect('mcn' in session).toBe(false);
  });

  it('does not expose a historical BI report for a different candidate version', () => {
    const session = toSession({
      id: 's-2', title: '示例品牌-夏季选人', brand: '示例品牌', campaign_name: '夏季选人',
      status: 'completed', platforms: ['bilibili'], category: '美妆护肤', target_audience: '18-30 岁女性',
      budget_min: null, budget_max: null, filters: {}, is_starred: false, messages: [],
      latest_task: { id: 'task-2', status: 'completed', completed_at: '2026-07-15T10:00:00Z' },
      latest_candidates: { task_id: 'task-2', version: 2, total: 3 },
      latest_report: {
        id: 'report-1', task_id: 'task-2', report_version: 1, candidate_version: 1,
        status: 'completed', generated_at: '2026-07-15T09:00:00Z',
      },
      created_at: '2026-07-14T10:00:00Z', updated_at: '2026-07-15T10:00:00Z',
    });

    expect(session.analysis?.candidateVersion).toBe(2);
    expect(session.analysis?.reportId).toBeUndefined();
  });

  it('restores follow-up suggestions from the latest task metadata', () => {
    const session = toSession({
      id: 's-follow', title: '建议', brand: '品牌', campaign_name: null, status: 'completed',
      platforms: ['douyin'], category: '餐饮', target_audience: '', budget_min: null, budget_max: null,
      filters: {}, is_starred: false, messages: [],
      latest_task: {
        id: 'task-follow', status: 'completed', completed_at: null,
        followup_suggestions_status: 'completed',
        followup_suggestions: [{ title: '分析地域', prompt: '请分析浙江粉丝', rationale: '优化投放区域' }],
        followup_error: null,
      },
      created_at: '2026-07-14T10:00:00Z', updated_at: '2026-07-15T10:00:00Z',
    });

    expect(session.analysis?.followupStatus).toBe('completed');
    expect(session.analysis?.followupSuggestions?.[0]?.prompt).toBe('请分析浙江粉丝');
  });

  it('deletes a session through the session endpoint', async () => {
    vi.mocked(request).mockResolvedValue(undefined);

    await deleteSession('session/with space');

    expect(request).toHaveBeenCalledWith('/api/v1/sessions/session%2Fwith%20space', {
      method: 'DELETE',
    });
  });
});
