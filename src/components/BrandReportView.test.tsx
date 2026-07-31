import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { ApiAnalysisReport, BrandReportPayload } from '../api/contracts';
import { analysisReportFixture, brandReportPayloadFixture } from '../test/fixtures';
import BrandReportView from './BrandReportView';

function brandV2Report(payload: BrandReportPayload): ApiAnalysisReport {
  return analysisReportFixture({
    id: 'brand-report-v2',
    report_type: 'brand_analysis',
    title: '海底捞品牌分析',
    template_version: 'brand_report_v2',
    payload,
  });
}

function renderView(payload: BrandReportPayload) {
  return render(<BrandReportView report={brandV2Report(payload)} />);
}

function chapter(container: HTMLElement, key: string): HTMLElement {
  const node = container.querySelector(`[data-chapter="${key}"]`);
  if (!node) throw new Error(`chapter ${key} not found`);
  return node as HTMLElement;
}

describe('BrandReportView', () => {
  it('renders all eight chapters with narrative sections placed after the data chapters', () => {
    const payload = brandReportPayloadFixture();
    // not_requested 的对比期不展示数值（把总互动的同比置为未请求）。
    payload.data.overview.total_interactions.yoy = { value: null, status: 'not_requested', reason: null };
    const { container } = renderView(payload);

    const nav = screen.getByRole('navigation', { name: '报告章节' });
    const navButtons = within(nav).getAllByRole('button');
    expect(navButtons.map(button => button.textContent)).toEqual([
      '概览', '情感', '趋势', '内容与达人', '地域', '热帖', '舆情', '方法论',
    ]);

    for (const key of ['overview', 'sentiment', 'daily_trend', 'content_creators', 'regions', 'top_posts', 'insights', 'methodology']) {
      expect(chapter(container, key)).toBeInTheDocument();
    }

    // 概览：指标对比（环比/同比百分比由后端算好，前端只展示）。
    const overview = chapter(container, 'overview');
    expect(within(overview).getByText('总声量')).toBeVisible();
    expect(within(overview).getByText('环比 +11.1%')).toBeVisible();
    expect(within(overview).getByText('同比 +33.3%')).toBeVisible();
    // not_requested 的对比期不展示数值。
    expect(within(overview).getByText('同比 未取数')).toBeVisible();

    // 叙事章节：AI 结论与结论与建议置于全部数据章节之后。
    const conclusion = screen.getByText('AI 结论');
    expect(screen.getByText('品牌整体声量健康，负面集中于排队体验。')).toBeVisible();
    expect(screen.getByText('结论与建议')).toBeVisible();
    expect(screen.getByText('优化排队体验')).toBeVisible();
    const methodology = chapter(container, 'methodology');
    expect(methodology.compareDocumentPosition(conclusion) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('shows the restricted reason for an unavailable trend chapter without fabricating a chart', () => {
    const base = brandReportPayloadFixture();
    const payload = brandReportPayloadFixture({
      data_status: 'partial',
      availability: {
        ...base.availability,
        daily_trend: { status: 'unavailable', missing_fields: ['points'], reason: 'no_evidence', source_tools: [], collected_at: null },
      },
    });
    const { container } = renderView(payload);

    const trend = chapter(container, 'daily_trend');
    expect(within(trend).getByText('受限')).toBeVisible();
    expect(within(trend).getByText(/未采集到相关证据/)).toBeVisible();
    // 禁止用概览变化字段伪造折线：受限章节不渲染任何趋势图与峰值。
    expect(within(trend).queryByLabelText('日趋势图表')).not.toBeInTheDocument();
    expect(within(trend).queryByText(/峰值/)).not.toBeInTheDocument();
  });

  it('marks a partial chapter as restricted with the missing fields', () => {
    const base = brandReportPayloadFixture();
    const payload = brandReportPayloadFixture({
      data_status: 'partial',
      availability: {
        ...base.availability,
        overview: { status: 'partial', missing_fields: ['total_interactions'], reason: 'no_data', source_tools: ['brand_mention_query'], collected_at: null },
      },
    });
    const { container } = renderView(payload);

    const overview = chapter(container, 'overview');
    expect(within(overview).getByText('受限')).toBeVisible();
    expect(within(overview).getByText(/查询无数据/)).toBeVisible();
    expect(within(overview).getByText(/total_interactions/)).toBeVisible();
  });

  it('switches top post platforms and labels exposure/share per platform', () => {
    const { container } = renderView(brandReportPayloadFixture());
    const topPosts = chapter(container, 'top_posts');

    // 默认小红书：阅读数 + 转发；抖音帖子不展示。
    expect(within(topPosts).getByText(/海底捞隐藏吃法合集/)).toBeVisible();
    expect(within(topPosts).queryByText(/海底捞生日歌现场/)).not.toBeInTheDocument();
    expect(within(topPosts).getByText('阅读数')).toBeVisible();
    expect(within(topPosts).getByText('转发')).toBeVisible();

    fireEvent.click(within(topPosts).getByRole('button', { name: '抖音' }));

    expect(within(topPosts).getByText(/海底捞生日歌现场/)).toBeVisible();
    expect(within(topPosts).queryByText(/海底捞隐藏吃法合集/)).not.toBeInTheDocument();
    // 抖音口径：播放数 + 分享。
    expect(within(topPosts).getByText('播放数')).toBeVisible();
    expect(within(topPosts).getByText('分享')).toBeVisible();
  });

  it('renders 未提供 for null fields and no source link when url is null', () => {
    const base = brandReportPayloadFixture();
    const payload = brandReportPayloadFixture({
      data: {
        ...base.data,
        top_posts: [{
          platform: 'xiaohongshu',
          post_id: null,
          collected_at: null,
          title: null,
          author: null,
          interactions: null,
          exposure_count: null,
          like_count: null,
          comment_count: null,
          collect_count: null,
          share_count: null,
          sentiment: null,
          creator_tier: null,
          url: null,
        }],
      },
    });
    const { container } = renderView(payload);
    const topPosts = chapter(container, 'top_posts');

    expect(within(topPosts).getAllByText('未提供').length).toBeGreaterThan(0);
    expect(within(topPosts).queryByRole('link')).not.toBeInTheDocument();

    // 展开后仍无跳转按钮（url 为 null）。
    fireEvent.click(within(topPosts).getByRole('button', { name: '展开' }));
    expect(within(topPosts).queryByRole('link')).not.toBeInTheDocument();
  });

  it('expands a clamped title to full text and reveals the source link', () => {
    const { container } = renderView(brandReportPayloadFixture());
    const topPosts = chapter(container, 'top_posts');

    const title = within(topPosts).getByText(/海底捞隐藏吃法合集/);
    expect(title).toHaveClass('line-clamp-2');
    expect(within(topPosts).queryByRole('link')).not.toBeInTheDocument();
    expect(within(topPosts).getByRole('button', { name: '展开' })).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(within(topPosts).getByRole('button', { name: '展开' }));

    expect(title).not.toHaveClass('line-clamp-2');
    expect(within(topPosts).getByRole('button', { name: '收起' })).toHaveAttribute('aria-expanded', 'true');
    const link = within(topPosts).getByRole('link', { name: '查看原帖' });
    expect(link).toHaveAttribute('href', 'https://www.xiaohongshu.com/explore/xhs-1');
    expect(link).toHaveAttribute('target', '_blank');

    fireEvent.click(within(topPosts).getByRole('button', { name: '收起' }));
    expect(title).toHaveClass('line-clamp-2');
    expect(within(topPosts).getByRole('button', { name: '展开' })).toHaveAttribute('aria-expanded', 'false');
  });

  it('scrolls to the chapter and marks it active from the anchor nav', () => {
    const scrollIntoView = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      renderView(brandReportPayloadFixture());
      const nav = screen.getByRole('navigation', { name: '报告章节' });
      const overviewButton = within(nav).getByRole('button', { name: '概览' });
      const topPostsButton = within(nav).getByRole('button', { name: '热帖' });
      expect(overviewButton).toHaveAttribute('aria-current', 'true');

      fireEvent.click(topPostsButton);

      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(topPostsButton).toHaveAttribute('aria-current', 'true');
      expect(overviewButton).toHaveAttribute('aria-current', 'false');
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  it('keeps methodology collapsed by default and expands on click', () => {
    const { container } = renderView(brandReportPayloadFixture());
    const methodology = chapter(container, 'methodology');

    expect(within(methodology).queryByText(/环比 2026-05，同比 2025-06/)).not.toBeInTheDocument();
    expect(within(methodology).queryByText(/brand_mention_query/)).not.toBeInTheDocument();

    fireEvent.click(within(methodology).getByRole('button', { name: '展开方法论' }));

    expect(within(methodology).getByText(/环比 2026-05，同比 2025-06/)).toBeVisible();
    expect(within(methodology).getByText(/brand_mention_query/)).toBeVisible();
  });

  it('falls back to an empty note when the payload is missing', () => {
    render(<BrandReportView report={analysisReportFixture({ template_version: null, payload: null })} />);

    expect(screen.getByText('报告内容为空')).toBeVisible();
  });

  it('falls back to an empty note instead of crashing when the payload shape is malformed', () => {
    const malformed = {
      ...brandReportPayloadFixture(),
      data: { overview: null, top_posts: 'truncated' },
    };
    render(
      <BrandReportView
        report={analysisReportFixture({
          template_version: 'brand_report_v2',
          payload: malformed as unknown as BrandReportPayload,
        })}
      />,
    );

    expect(screen.getByText('报告内容为空')).toBeVisible();
    expect(screen.queryByRole('navigation', { name: '报告章节' })).not.toBeInTheDocument();
  });
});
