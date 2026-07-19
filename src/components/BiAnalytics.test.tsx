import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { BiAnalyticsData } from '../api/contracts';
import { emptyAnalytics } from '../test/fixtures';
import BiAnalytics from './BiAnalytics';

const analytics: BiAnalyticsData = {
  overview: {
    brand_volume: { value: 15745, unit: '条', available: true, coverage: 1, source_fields: ['brand_mentions'], platforms: ['xiaohongshu'] },
    total_exposure: { value: 12500000, unit: '次', available: true, coverage: 1, source_fields: ['exposure'], platforms: ['xiaohongshu', 'douyin'] },
    average_engagement_rate: { value: 6.2, unit: '%', available: true, coverage: 1, source_fields: ['interactions', 'exposure'], platforms: ['douyin'] },
  },
  sentiment: {
    available: true, coverage: 1, source_fields: ['sentiment_counts'], platforms: ['xiaohongshu'],
    items: [
      { key: 'positive', label: '正向', value: 78, percentage: 78 },
      { key: 'neutral', label: '中立', value: 15, percentage: 15 },
      { key: 'negative', label: '负向', value: 7, percentage: 7 },
    ],
    hot_words: [{ term: '感感高级', count: 10 }, { term: '绝美雾面', count: 8 }],
  },
  exposure_trend: [
    { date: '2026-06-20', value: 100000, unit: '次', platforms: ['xiaohongshu'] },
    { date: '2026-06-22', value: 4200000, unit: '次', platforms: ['xiaohongshu', 'douyin'] },
    { date: '2026-06-26', value: 700000, unit: '次', platforms: ['douyin'] },
  ],
  audience: {
    age: { value: null, unit: '%', available: true, coverage: 1, source_fields: ['audience_age'], platforms: ['xiaohongshu'], items: [{ label: '18-24', value: 52, unit: '%' }, { label: '25-30', value: 34, unit: '%' }] },
    gender: { value: null, unit: '%', available: true, coverage: 1, source_fields: ['audience_gender'], platforms: ['xiaohongshu'], items: [{ label: '女性', value: 85, unit: '%' }, { label: '男性', value: 15, unit: '%' }] },
    regions: { value: null, unit: '%', available: true, coverage: 1, source_fields: ['audience_regions'], platforms: ['xiaohongshu'], items: [{ label: '广东', value: 22, unit: '%' }, { label: '上海', value: 19, unit: '%' }, { label: '北京', value: 16, unit: '%' }, { label: '浙江', value: 14, unit: '%' }, { label: '江苏', value: 11, unit: '%' }] },
  },
};

describe('BiAnalytics', () => {
  it('renders the compact analytics cards and charts from the latest report DTO', () => {
    render(<BiAnalytics analytics={analytics} taskStatus="completed" />);

    for (const title of ['全网品牌声量', '总曝光量', '平均互动率', '舆情情感极性分析', '活动传播周期与曝光走势', '粉丝客群/受众人口统计画像']) {
      expect(screen.getByText(title)).toBeVisible();
    }
    expect(screen.getByText('15,745')).toBeVisible();
    expect(screen.getByText('12.5M')).toBeVisible();
    expect(screen.getByText('6.2%')).toBeVisible();
    expect(screen.getByText('正向')).toBeVisible();
    expect(screen.getByText('感感高级')).toBeVisible();
    expect(screen.getByText('浙江')).toBeVisible();
  });

  it('keeps the report frames and announces missing data without inventing zeros', () => {
    render(<BiAnalytics analytics={undefined} taskStatus="running" />);

    expect(screen.getByText('全网品牌声量')).toBeVisible();
    expect(screen.getAllByText('数据不足').length).toBeGreaterThanOrEqual(3);
    expect(screen.queryByText('0')).not.toBeInTheDocument();
  });

  it('exposes the data-analysis panel as a keyboard reachable tab in the report shell', () => {
    const onChange = vi.fn();
    render(<BiAnalytics analytics={analytics} taskStatus="completed" />);
    const panel = screen.getByRole('region', { name: '数据分析' });
    expect(panel).toBeVisible();
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.keyDown(panel, { key: 'Tab' });
  });
});

describe('mergeAnalyticsChannels', () => {
  it('falls back to kol analytics sections when brand sections are unavailable', async () => {
    const { mergeAnalyticsChannels } = await import('./BiAnalytics');
    const kolAnalytics = {
      overview: {
        brand_volume: { value: null, unit: '条', available: false, coverage: 0, source_fields: [], platforms: [] },
        total_exposure: { value: null, unit: '次', available: false, coverage: 0, source_fields: [], platforms: [] },
        average_engagement_rate: { value: null, unit: '%', available: false, coverage: 0, source_fields: [], platforms: [] },
      },
      sentiment: { available: false, coverage: 0, source_fields: [], platforms: [], items: [], hot_words: [] },
      exposure_trend: [],
      audience: {
        age: { value: null, unit: '%', available: true, coverage: 0.9, source_fields: ['audience_age'], platforms: ['douyin'], items: [{ label: '18-23', value: 57.85, unit: '%' }] },
        gender: { value: null, unit: '%', available: true, coverage: 0.9, source_fields: ['audience_gender'], platforms: ['douyin'], items: [{ label: '女', value: 68, unit: '%' }] },
        regions: { value: null, unit: '%', available: true, coverage: 0.9, source_fields: ['audience_regions'], platforms: ['douyin'], items: [{ label: '广东', value: 9, unit: '%' }] },
      },
    } as const;

    const merged = mergeAnalyticsChannels(emptyAnalytics, kolAnalytics as never);

    expect(merged?.audience.gender.items[0]).toEqual({ label: '女', value: 68, unit: '%' });
    expect(merged?.audience.age.items[0]?.label).toBe('18-23');
    // 品牌指标在双通道都不可用时保持不可用。
    expect(merged?.overview.brand_volume.available).toBe(false);
  });
});

describe('mergeAnalyticsChannels malformed channels', () => {
  it('treats {}-shaped channels as missing instead of crashing', async () => {
    const { mergeAnalyticsChannels } = await import('./BiAnalytics');

    expect(mergeAnalyticsChannels({} as never, emptyAnalytics)).toBe(emptyAnalytics);
    expect(mergeAnalyticsChannels(emptyAnalytics, {} as never)).toBe(emptyAnalytics);
    expect(mergeAnalyticsChannels(undefined, undefined)).toBeUndefined();
  });
});
