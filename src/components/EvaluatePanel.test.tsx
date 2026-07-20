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

function selectFile(file: File) {
  fireEvent.change(screen.getByLabelText('选择数据表格'), { target: { files: [file] } });
}

describe('EvaluatePanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockPostEvaluate.mockResolvedValue({ title: '火锅活动评估', analysis_markdown: '**热度很高**\n\n持续上升' });
  });

  it('opens the upload modal on entry and rejects files over 5MB', () => {
    render(<EvaluatePanel onBack={vi.fn()} />);

    expect(screen.getByText('上传数据表格')).toBeTruthy();
    const bigFile = new File([new Uint8Array(6 * 1024 * 1024)], 'big.xlsx');
    selectFile(bigFile as File);

    expect(screen.getByText('文件不能超过 5MB')).toBeTruthy();
    expect(screen.getByRole('button', { name: '开始评估' })).toBeDisabled();
  });

  it('rejects unsupported file extensions', () => {
    render(<EvaluatePanel onBack={vi.fn()} />);

    selectFile(new File(['plain'], 'notes.txt', { type: 'text/plain' }));

    expect(screen.getByText('仅支持 xlsx 或 csv 文件')).toBeTruthy();
    expect(screen.getByRole('button', { name: '开始评估' })).toBeDisabled();
  });

  it('uploads the selected file and renders the markdown analysis', async () => {
    render(<EvaluatePanel onBack={vi.fn()} />);

    const file = new File(['nick,interact\n达人甲,1000'], 'campaign.csv', { type: 'text/csv' });
    selectFile(file);
    fireEvent.click(screen.getByRole('button', { name: '开始评估' }));

    expect(await screen.findByText('火锅活动评估')).toBeTruthy();
    expect(mockPostEvaluate).toHaveBeenCalledTimes(1);
    expect(mockPostEvaluate.mock.calls[0]?.[0]).toBe(file);
    // markdown 渲染复用报告样式（whitespace-pre-wrap 保留换行）
    expect(screen.getByText(/热度很高/)).toBeTruthy();
    // 成功后上传对话框关闭
    expect(screen.queryByText('上传数据表格')).toBeNull();
  });

  it('keeps the modal open and shows the error when the upload fails', async () => {
    mockPostEvaluate.mockRejectedValue(new Error('QUICK_CALL_FAILED'));
    render(<EvaluatePanel onBack={vi.fn()} />);

    selectFile(new File(['a,b\n1,2'], 'campaign.csv', { type: 'text/csv' }));
    fireEvent.click(screen.getByRole('button', { name: '开始评估' }));

    await waitFor(() => {
      expect(screen.getAllByText('评估失败，请稍后重试').length).toBeGreaterThan(0);
    });
    expect(screen.getByText('上传数据表格')).toBeTruthy();
  });

  it('goes back to the session view', () => {
    const onBack = vi.fn();
    render(<EvaluatePanel onBack={onBack} />);

    fireEvent.click(screen.getByRole('button', { name: /返回会话/ }));

    expect(onBack).toHaveBeenCalledTimes(1);
  });
});
