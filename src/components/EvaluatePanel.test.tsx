import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { postEvaluate } from '../api/quick';
import EvaluatePanel from './EvaluatePanel';

vi.mock('../api/quick', () => ({
  postEvaluate: vi.fn(),
  quickErrorMessage: (error: unknown) =>
    error instanceof Error && error.message === 'INSUFFICIENT_POINTS' ? '积分不足，请充值' : '评估失败，请稍后重试',
}));

const mockPostEvaluate = vi.mocked(postEvaluate);

function fillActivityName(value: string) {
  fireEvent.change(screen.getByLabelText('活动名称'), { target: { value } });
}

function kolNameInput() {
  return screen.getByLabelText('达人名称');
}

function addKolName(name: string) {
  fireEvent.change(kolNameInput(), { target: { value: name } });
  fireEvent.keyDown(kolNameInput(), { key: 'Enter' });
}

describe('EvaluatePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPostEvaluate.mockResolvedValue({ title: '火锅活动评估', analysis_markdown: '**热度很高**\n\n持续上升' });
  });

  it('keeps the submit button disabled until an activity name and at least one kol are entered', () => {
    render(<EvaluatePanel />);

    const submit = screen.getByRole('button', { name: '开始评估' });
    expect(submit).toBeDisabled();

    fillActivityName('火锅节活动');
    expect(submit).toBeDisabled();

    addKolName('达人甲');
    expect(submit).toBeEnabled();
  });

  it('adds kol names as chips on enter, comma and blur, deduplicates and removes chips', () => {
    render(<EvaluatePanel />);

    addKolName('达人甲');
    // 逗号分隔添加
    fireEvent.change(kolNameInput(), { target: { value: '达人乙,' } });
    // 失焦添加
    fireEvent.change(kolNameInput(), { target: { value: ' 达人丙 ' } });
    fireEvent.blur(kolNameInput());
    // 去重：重复输入不新增
    addKolName('达人甲');

    expect(screen.getByText('达人甲')).toBeTruthy();
    expect(screen.getByText('达人乙')).toBeTruthy();
    expect(screen.getByText('达人丙')).toBeTruthy();
    expect(screen.getAllByRole('button', { name: /^移除 / })).toHaveLength(3);

    fireEvent.click(screen.getByRole('button', { name: '移除 达人乙' }));
    expect(screen.queryByText('达人乙')).toBeNull();
  });

  it('shows a hint when more than 20 kol names are entered', () => {
    render(<EvaluatePanel />);

    for (let index = 1; index <= 20; index += 1) {
      addKolName(`达人${index}`);
    }
    addKolName('达人21');

    expect(screen.getByText(/最多添加 20 位达人/)).toBeTruthy();
    expect(screen.getAllByRole('button', { name: /^移除 / })).toHaveLength(20);
  });

  it('submits the JSON payload and renders the markdown analysis', async () => {
    render(<EvaluatePanel />);

    fillActivityName('火锅节活动');
    addKolName('达人甲');
    addKolName('达人乙');
    fireEvent.click(screen.getByRole('button', { name: '开始评估' }));

    expect(await screen.findByText('火锅活动评估')).toBeTruthy();
    expect(mockPostEvaluate).toHaveBeenCalledTimes(1);
    expect(mockPostEvaluate).toHaveBeenCalledWith({ activityName: '火锅节活动', kolNames: ['达人甲', '达人乙'] });
    // markdown 渲染复用报告样式（whitespace-pre-wrap 保留换行）
    expect(screen.getByText(/热度很高/)).toBeTruthy();
  });

  it('shows the quick error message when the evaluate request fails', async () => {
    mockPostEvaluate.mockRejectedValue(new Error('INSUFFICIENT_POINTS'));
    render(<EvaluatePanel />);

    fillActivityName('火锅节活动');
    addKolName('达人甲');
    fireEvent.click(screen.getByRole('button', { name: '开始评估' }));

    expect(await screen.findByText('积分不足，请充值')).toBeTruthy();
  });

  it('clears the result but keeps the inputs when re-evaluating', async () => {
    render(<EvaluatePanel />);

    fillActivityName('火锅节活动');
    addKolName('达人甲');
    fireEvent.click(screen.getByRole('button', { name: '开始评估' }));
    expect(await screen.findByText('火锅活动评估')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '重新评估' }));

    expect(screen.queryByText('火锅活动评估')).toBeNull();
    expect((screen.getByLabelText('活动名称') as HTMLInputElement).value).toBe('火锅节活动');
    expect(screen.getByText('达人甲')).toBeTruthy();
    expect(screen.getByRole('button', { name: '开始评估' })).toBeEnabled();
  });

  it('shows the long-running hint while evaluating', async () => {
    let resolveEvaluate: (value: { title: string; analysis_markdown: string }) => void = () => undefined;
    mockPostEvaluate.mockImplementation(
      () => new Promise(resolve => {
        resolveEvaluate = resolve;
      }),
    );
    render(<EvaluatePanel />);

    fillActivityName('火锅节活动');
    addKolName('达人甲');
    fireEvent.click(screen.getByRole('button', { name: '开始评估' }));

    expect(await screen.findByText(/评估中，可能需要几分钟/)).toBeTruthy();
    resolveEvaluate({ title: '火锅活动评估', analysis_markdown: 'ok' });
    await waitFor(() => expect(screen.queryByText(/评估中，可能需要几分钟/)).toBeNull());
  });
});
