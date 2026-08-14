import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { InsightBoardPayload } from '../../api/agentArtifacts';
import InsightBoardView from './InsightBoardView';

function boardPayload(): InsightBoardPayload {
  return {
    schema_version: 'insight_board_v1',
    title: '竞品钻取',
    data_status: 'complete',
    availability: { blocks: { status: 'complete', reason_codes: [] } },
    limitations: [],
    methodology: { data_as_of: '2026-08-01T10:00:00', source_names: ['xiaohongshu'], notes: [] },
    scope: { summary: '', period: null, platforms: [], brand: '竞品', campaign: null, kol_uid: null },
    parent_artifact_id: 'brand-1',
    narrative: { summary: '钻取说明', findings: [] },
    data: [{
      block_type: 'references',
      title: '参考来源',
      references: [
        { label: '合规来源', url: 'https://example.com/a' },
        { label: '恶意链接', url: 'javascript:alert(1)' },
        { label: '无链接条目' },
      ],
    }],
  };
}

describe('InsightBoardView', () => {
  it('references 只把 http/https 渲染为链接，其他协议降级为文本（URL 白名单）', () => {
    render(<InsightBoardView payload={boardPayload()} />);

    expect(screen.getByRole('link', { name: '合规来源' })).toHaveAttribute('href', 'https://example.com/a');
    expect(screen.queryByRole('link', { name: '恶意链接' })).not.toBeInTheDocument();
    expect(screen.getByText('恶意链接')).toBeVisible();
    expect(screen.getByText('无链接条目')).toBeVisible();
  });
});
