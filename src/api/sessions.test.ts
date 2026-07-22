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
      is_starred: false, kol_selection_count: 0,
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

  it('tolerates null brand/category from blank sessions', () => {
    const session = toSession({
      id: 's-blank',
      title: '新会话1',
      brand: null,
      campaign_name: null,
      status: 'draft',
      platforms: [],
      category: null,
      target_audience: '',
      budget_min: null,
      budget_max: null,
      filters: {},
      is_starred: false, kol_selection_count: 0,
      messages: [],
      created_at: '2026-07-21T10:00:00Z',
      updated_at: '2026-07-21T10:00:00Z',
    });

    expect(session.brand).toBe('');
    expect(session.category).toBe('');
    expect(session.platform).toBe('');
  });

  it('exposes the latest analysis report only when it belongs to the latest task', () => {
    const base = {
      id: 's-2', title: '示例品牌-夏季选人', brand: '示例品牌', campaign_name: '夏季选人',
      status: 'completed' as const, platforms: ['bilibili'], category: '美妆护肤', target_audience: '18-30 岁女性',
      budget_min: null, budget_max: null, filters: {}, is_starred: false, kol_selection_count: 0, messages: [],
      latest_task: { id: 'task-2', status: 'completed' as const, completed_at: '2026-07-15T10:00:00Z' },
      created_at: '2026-07-14T10:00:00Z', updated_at: '2026-07-15T10:00:00Z',
    };
    const summary = {
      id: 'analysis-report-1', task_id: 'task-2', version: 1, title: '自由分析报告',
      status: 'completed', generated_at: '2026-07-15T09:00:00Z',
    };

    expect(toSession({ ...base, latest_analysis_report: summary }).analysis?.analysisReportId).toBe('analysis-report-1');
    expect(toSession({ ...base, latest_analysis_report: { ...summary, task_id: 'task-other' } }).analysis?.analysisReportId).toBeUndefined();
  });

  it('accepts session-level analysis reports whose task_id is null', () => {
    const session = toSession({
      id: 's-kol', title: '圈选会话', brand: '示例品牌', campaign_name: null, status: 'completed',
      platforms: ['xiaohongshu'], category: '美妆护肤', target_audience: '', budget_min: null, budget_max: null,
      filters: {}, is_starred: false, kol_selection_count: 7, messages: [],
      latest_task: { id: 'task-9', status: 'completed' as const, completed_at: '2026-07-21T10:00:00Z' },
      latest_analysis_report: {
        id: 'analysis-report-kol', task_id: null, version: 1, title: 'KOL 匹配度分析',
        status: 'completed', generated_at: '2026-07-21T10:00:00Z',
      },
      created_at: '2026-07-21T09:00:00Z', updated_at: '2026-07-21T10:00:00Z',
    });

    expect(session.analysis?.analysisReportId).toBe('analysis-report-kol');
    expect(session.kolSelectionCount).toBe(7);
  });

  it('restores follow-up suggestions from the latest task metadata', () => {
    const session = toSession({
      id: 's-follow', title: '建议', brand: '品牌', campaign_name: null, status: 'completed',
      platforms: ['douyin'], category: '餐饮', target_audience: '', budget_min: null, budget_max: null,
      filters: {}, is_starred: false, kol_selection_count: 0, messages: [],
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

  it('maps brainstorm metadata onto messages', () => {
    const session = toSession({
      id: 's-brainstorm',
      title: '新会话1',
      brand: '',
      campaign_name: null,
      status: 'draft',
      platforms: [],
      category: '',
      target_audience: '',
      budget_min: null,
      budget_max: null,
      filters: {},
      is_starred: false, kol_selection_count: 0,
      messages: [{
        id: 'm-1',
        role: 'assistant',
        content: '想分析哪个平台？',
        sequence: 1,
        metadata: {
          brainstorm: {
            ready: false,
            options: ['小红书', '抖音'],
            profile_summary: null,
          },
        },
        created_at: '2026-07-20T10:00:00Z',
      }],
      created_at: '2026-07-20T10:00:00Z',
      updated_at: '2026-07-20T10:00:00Z',
    });

    expect(session.messages[0].brainstorm).toEqual({
      ready: false,
      options: ['小红书', '抖音'],
      profile_summary: null,
    });
  });

  it('deletes a session through the session endpoint', async () => {
    vi.mocked(request).mockResolvedValue(undefined);

    await deleteSession('session/with space');

    expect(request).toHaveBeenCalledWith('/api/v1/sessions/session%2Fwith%20space', {
      method: 'DELETE',
    });
  });
});
