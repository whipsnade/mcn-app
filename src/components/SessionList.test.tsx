import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { describe, expect, it, vi } from 'vitest';

import type { Session } from '../types';
import SessionList from './SessionList';


const baseSession: Session = {
  id: 'session-1',
  title: ' 自定义会话名 ',
  brand: '示例品牌',
  campaignName: '夏季种草',
  status: 'draft',
  platform: 'Xiaohongshu',
  category: '美妆',
  targetAudience: '年轻女性',
  summary: '筛选达人',
  messages: [],
  isStarred: false,
  createdAt: '2026-07-14T10:00:00Z',
  updatedAt: '2026-07-14T10:00:00Z',
};

function renderList(overrides: Partial<ComponentProps<typeof SessionList>> = {}) {
  const props: ComponentProps<typeof SessionList> = {
    sessions: [baseSession],
    activeSessionId: baseSession.id,
    onSelectSession: vi.fn(),
    onOpenNewModal: vi.fn(),
    user: { nickname: '测试用户', role: 'user' },
    points: null,
    onOpenRecharge: vi.fn(),
    ...overrides,
  };
  return { ...render(<SessionList {...props} />), props };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  return {
    promise: new Promise<T>(next => { resolve = next; }),
    resolve,
  };
}


describe('SessionList', () => {
  it('does not report a zero balance when the wallet is unavailable', () => {
    renderList({ sessions: [] });

    expect(screen.getByText('积分暂不可用')).toBeTruthy();
    expect(screen.queryByText(/0 \/ 5,000 点/)).toBeNull();
  });

  it('uses stable session titles and deterministic metadata fallbacks', () => {
    const sessions: Session[] = [
      baseSession,
      { ...baseSession, id: 'session-2', title: ' ', brand: '品牌甲', campaignName: '活动乙' },
      { ...baseSession, id: 'session-3', title: '', brand: '', campaignName: null, category: '食品' },
      { ...baseSession, id: 'session-4', title: '', brand: '', campaignName: null, category: '' },
    ];
    const { rerender, props } = renderList({ sessions });

    expect(screen.getByText('自定义会话名')).toBeTruthy();
    expect(screen.getByText('品牌甲 - 活动乙')).toBeTruthy();
    expect(screen.getByText('食品 KOL 分析')).toBeTruthy();
    expect(screen.getByText('未命名会话')).toBeTruthy();

    rerender(<SessionList {...props} sessions={[{
      ...baseSession,
      status: 'completed',
      messages: [{ id: 'message-new', sender: 'ai', text: '任务已完成', timestamp: '12:00' }],
    }]} />);
    expect(screen.getByText('自定义会话名')).toBeTruthy();
  });

  it('opens and cancels inline deletion without selecting or deleting the session', () => {
    const onSelectSession = vi.fn();
    const onDeleteSession = vi.fn();
    renderList({ onSelectSession, onDeleteSession });

    fireEvent.click(screen.getByRole('button', { name: '删除会话 自定义会话名' }));
    expect(screen.getByText('确定删除这个会话吗？')).toBeTruthy();
    expect(onSelectSession).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '取消删除' }));
    expect(screen.queryByText('确定删除这个会话吗？')).toBeNull();
    expect(onDeleteSession).not.toHaveBeenCalled();
    expect(onSelectSession).not.toHaveBeenCalled();
  });

  it('disables duplicate confirmation while deletion is in flight', async () => {
    const pending = deferred<void>();
    const onDeleteSession = vi.fn(() => pending.promise);
    renderList({ onDeleteSession });

    fireEvent.click(screen.getByRole('button', { name: '删除会话 自定义会话名' }));
    const confirm = screen.getByRole('button', { name: '确认删除' });
    fireEvent.click(confirm);
    fireEvent.click(confirm);

    expect(onDeleteSession).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: '删除中' })).toBeDisabled();

    pending.resolve();
    await waitFor(() => expect(screen.queryByText('确定删除这个会话吗？')).toBeNull());
  });

  it('does not allow another session confirmation while deletion is in flight', async () => {
    const pending = deferred<void>();
    const secondSession = { ...baseSession, id: 'session-2', title: '第二个会话' };
    const onDeleteSession = vi.fn(() => pending.promise);
    renderList({ sessions: [baseSession, secondSession], onDeleteSession });

    fireEvent.click(screen.getByRole('button', { name: '删除会话 自定义会话名' }));
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));

    const secondTrigger = screen.getByRole('button', { name: '删除会话 第二个会话' });
    expect(secondTrigger).toBeDisabled();
    fireEvent.click(secondTrigger);
    expect(screen.getAllByRole('alertdialog')).toHaveLength(1);
    expect(onDeleteSession).toHaveBeenCalledWith('session-1');
    expect(onDeleteSession).toHaveBeenCalledTimes(1);

    pending.resolve();
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull());
  });

  it('provides alert dialog semantics, focus management, and Escape cancellation', () => {
    renderList({ onDeleteSession: vi.fn() });
    const trigger = screen.getByRole('button', { name: '删除会话 自定义会话名' });

    fireEvent.click(trigger);

    const dialog = screen.getByRole('alertdialog', { name: '确定删除这个会话吗？' });
    expect(dialog).toHaveAccessibleDescription('删除后无法恢复。');
    expect(screen.getByRole('button', { name: '确认删除' })).toHaveFocus();

    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it('shows a Chinese inline error and allows retry after deletion fails', async () => {
    const onDeleteSession = vi.fn().mockRejectedValue(new Error('network failed'));
    renderList({ onDeleteSession });

    fireEvent.click(screen.getByRole('button', { name: '删除会话 自定义会话名' }));
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));

    await waitFor(() => expect(screen.getByText('删除会话失败，请稍后重试。')).toBeTruthy());
    expect(screen.getByRole('button', { name: '确认删除' })).not.toBeDisabled();
  });

  it('allows saving an empty project or campaign name', () => {
    const onRenameSession = vi.fn();
    renderList({ onRenameSession });

    fireEvent.click(screen.getByTitle('重命名会话'));
    fireEvent.change(screen.getByPlaceholderText('例如: 完美日记'), { target: { value: '' } });
    fireEvent.change(screen.getByPlaceholderText('例如: 新品宣发'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: '保存' }));

    expect(onRenameSession).toHaveBeenCalledWith('session-1', '', '');
  });
});
