import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ApiQuickKolRecommendations } from '../api/contracts';
import { getKolRecommendations } from '../api/quick';
import KolRecommendPanel from './KolRecommendPanel';

vi.mock('../api/quick', () => ({
  getKolRecommendations: vi.fn(),
  quickErrorMessage: (error: unknown) =>
    error instanceof Error && error.message === 'INSUFFICIENT_POINTS' ? '积分不足，请充值' : '查询失败，请稍后重试',
}));

const mockGetKolRecommendations = vi.mocked(getKolRecommendations);

const RESULT: ApiQuickKolRecommendations = {
  items: [
    {
      platform: 'xiaohongshu',
      kw_uid: 'uid-1',
      nickname: '美食达人甲',
      fans: 125_000,
      price: 30_000,
      engagement_rate: 5.2,
      score: 88,
      city: '上海',
      tags: ['美食'],
    },
    {
      platform: 'douyin',
      kw_uid: 'uid-2',
      nickname: '探店达人乙',
      fans: 8_000,
      price: null,
      engagement_rate: null,
      score: 70,
      city: null,
      tags: [],
    },
  ],
  points_cost: 20,
};

async function advanceDebounce(ms = 800) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe('KolRecommendPanel', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    mockGetKolRecommendations.mockResolvedValue(RESULT);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('fetches recommendations after the initial debounce and renders the list', async () => {
    render(<KolRecommendPanel onBack={vi.fn()} onSelectKol={vi.fn()} />);

    expect(mockGetKolRecommendations).not.toHaveBeenCalled();
    await advanceDebounce();

    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
    expect(mockGetKolRecommendations).toHaveBeenCalledWith({ budget: 50_000 });
    expect(screen.getByText('美食达人甲')).toBeTruthy();
    expect(screen.getByText('探店达人乙')).toBeTruthy();
    expect(screen.getByText('粉丝 12.5万')).toBeTruthy();
    expect(screen.getByText('¥30,000')).toBeTruthy();
    expect(screen.getByText('无报价')).toBeTruthy();
    expect(screen.getByText('互动率 5.2%')).toBeTruthy();
    expect(screen.getByText('上次消耗 20 积分', { exact: false })).toBeTruthy();
  });

  it('debounces slider changes and refetches with the new budget', async () => {
    render(<KolRecommendPanel onBack={vi.fn()} onSelectKol={vi.fn()} />);
    await advanceDebounce();
    mockGetKolRecommendations.mockClear();

    const slider = screen.getByLabelText('单达人报价预算');
    fireEvent.change(slider, { target: { value: '200000' } });
    fireEvent.change(slider, { target: { value: '300000' } });

    // 防抖窗口内连续变更只触发一次请求
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(mockGetKolRecommendations).not.toHaveBeenCalled();

    await advanceDebounce(400);
    expect(mockGetKolRecommendations).toHaveBeenCalledTimes(1);
    expect(mockGetKolRecommendations).toHaveBeenCalledWith({ budget: 300_000 });
    expect(screen.getByText('¥30.0万')).toBeTruthy();
  });

  it('reports the selected kol when a row is clicked', async () => {
    const onSelectKol = vi.fn();
    render(<KolRecommendPanel onBack={vi.fn()} onSelectKol={onSelectKol} />);
    await advanceDebounce();

    fireEvent.click(screen.getByText('美食达人甲'));

    expect(onSelectKol).toHaveBeenCalledWith({
      platform: 'xiaohongshu',
      kw_uid: 'uid-1',
      nickname: '美食达人甲',
    });
  });

  it('shows a recharge hint when points are insufficient', async () => {
    mockGetKolRecommendations.mockRejectedValue(new Error('INSUFFICIENT_POINTS'));
    render(<KolRecommendPanel onBack={vi.fn()} onSelectKol={vi.fn()} />);
    await advanceDebounce();

    expect(screen.getByText('积分不足，请充值')).toBeTruthy();
  });

  it('goes back to the session view', async () => {
    const onBack = vi.fn();
    render(<KolRecommendPanel onBack={onBack} onSelectKol={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: /返回会话/ }));

    expect(onBack).toHaveBeenCalledTimes(1);
  });
});
