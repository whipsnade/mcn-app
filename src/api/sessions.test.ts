import { describe, expect, it } from 'vitest';

import { toSession } from './sessions';


describe('toSession', () => {
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
});
