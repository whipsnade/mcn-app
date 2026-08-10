import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import ConfirmDialog from './ConfirmDialog';

const baseProps = {
  open: true,
  title: '确认操作',
  onConfirm: vi.fn(),
  onCancel: vi.fn(),
};

describe('ConfirmDialog', () => {
  it('renders nothing when closed', () => {
    render(<ConfirmDialog {...baseProps} open={false} />);
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('exposes dialog semantics with an accessible name', () => {
    render(<ConfirmDialog {...baseProps} description="操作说明" />);
    const dialog = screen.getByRole('dialog', { name: '确认操作' });
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(screen.getByText('操作说明')).toBeTruthy();
  });

  it('focuses the first actionable element on open', () => {
    render(<ConfirmDialog {...baseProps} />);
    const dialog = screen.getByRole('dialog');
    const focused = document.activeElement;
    expect(focused).not.toBeNull();
    expect(dialog.contains(focused)).toBe(true);
    expect((focused as HTMLElement).tagName).toBe('BUTTON');
  });

  it('closes on Escape without confirming', () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    render(<ConfirmDialog {...baseProps} onCancel={onCancel} onConfirm={onConfirm} />);
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('invokes confirm and cancel callbacks from buttons', () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    render(<ConfirmDialog {...baseProps} onCancel={onCancel} onConfirm={onConfirm} confirmLabel="确认执行" cancelLabel="再想想" />);
    fireEvent.click(screen.getByRole('button', { name: '确认执行' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: '再想想' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('traps Tab focus inside the dialog', () => {
    render(<ConfirmDialog {...baseProps} confirmLabel="确认" cancelLabel="取消" />);
    const confirmButton = screen.getByRole('button', { name: '确认' });
    const cancelButton = screen.getByRole('button', { name: '取消' });
    cancelButton.focus();
    // Tab 到最后一个元素后再按 Tab 应回到第一个
    const buttons = [confirmButton, cancelButton];
    const last = buttons[buttons.length - 1];
    last.focus();
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Tab' });
    expect(document.activeElement).toBe(buttons[0]);
    buttons[0].focus();
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Tab', shiftKey: true });
    expect(document.activeElement).toBe(last);
  });
});
