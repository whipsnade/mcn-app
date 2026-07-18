import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { analysisReportFixture } from '../test/fixtures';
import UniversalReport from './UniversalReport';

describe('UniversalReport', () => {
  it('renders every supported block type from the analysis report DTO', () => {
    render(<UniversalReport report={analysisReportFixture()} taskStatus="completed" />);

    expect(screen.getByText('品牌自由分析报告')).toBeVisible();
    // heading 与 markdown
    expect(screen.getByText('一、核心结论')).toBeVisible();
    expect(screen.getByText(/本次 campaign 整体声量向好/)).toBeVisible();
    // metric_grid：数字与字符串取值、delta
    expect(screen.getByText('总曝光量')).toBeVisible();
    expect(screen.getByText('12,500,000')).toBeVisible();
    expect(screen.getByText('高于大盘')).toBeVisible();
    expect(screen.getByText('+37.2%')).toBeVisible();
    // table：表头、单元格与 null 降级
    expect(screen.getByText('平台表现')).toBeVisible();
    expect(screen.getByText('互动率')).toBeVisible();
    expect(screen.getByText('9021')).toBeVisible();
    expect(screen.getByText('—')).toBeVisible();
    // 图表卡片标题
    expect(screen.getByText('平台声量对比')).toBeVisible();
    expect(screen.getByText('曝光走势')).toBeVisible();
    expect(screen.getByText('情感占比')).toBeVisible();
    // tag_list 与 sources
    expect(screen.getByText('质感高级')).toBeVisible();
    expect(screen.getByText('品牌声量查询')).toBeVisible();
    expect(screen.getByText(/证据编号：EV-001/)).toBeVisible();
    // 结论
    expect(screen.getByText('整体表现优于预期，建议加大小红书投放。')).toBeVisible();
  });

  it('skips blocks whose payload is empty or incomplete', () => {
    render(
      <UniversalReport
        report={analysisReportFixture({
          blocks: [
            { type: 'heading', text: '  ' },
            { type: 'markdown', text: '' },
            { type: 'metric_grid', title: '空指标', items: [] },
            { type: 'table', title: '空表格', columns: ['平台'], rows: [] },
            { type: 'bar_chart', title: '空柱状', categories: [], series: [{ name: '声量', values: [1] }] },
            { type: 'line_chart', title: '空折线', categories: ['06-20'], series: [{ name: '曝光', values: [1] }] },
            { type: 'pie_chart', title: '空饼图', categories: ['正向'], series: [{ name: '占比', values: [null] }] },
            { type: 'tag_list', title: '空热词', items: [] },
            { type: 'sources', items: [] },
          ],
          conclusion: null,
        })}
        taskStatus="completed"
      />,
    );

    for (const title of ['空指标', '空表格', '空柱状', '空折线', '空饼图', '空热词', '数据来源', 'AI 结论']) {
      expect(screen.queryByText(title)).not.toBeInTheDocument();
    }
    expect(screen.getByText('报告内容为空')).toBeVisible();
  });

  it('shows a placeholder while no analysis report exists', () => {
    render(<UniversalReport taskStatus="running" />);

    expect(screen.getByText('智能分析报告')).toBeVisible();
    expect(screen.getByText('正在生成分析报告…')).toBeVisible();
  });

  it('falls back to the idle placeholder without a task status', () => {
    render(<UniversalReport />);

    expect(screen.getByText('等待生成分析报告')).toBeVisible();
  });

  it('announces that report content may still change while the task runs', () => {
    render(<UniversalReport report={analysisReportFixture()} taskStatus="running" />);

    expect(screen.getByRole('status')).toHaveTextContent('任务进行中，报告内容可能继续更新');
    expect(screen.getByText('一、核心结论')).toBeVisible();
  });
});
