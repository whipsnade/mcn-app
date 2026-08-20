import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import AdminNavigation from './AdminNavigation';

describe('AdminNavigation', () => {
  it('exposes all B6 modules with an accessible current selection', () => {
    const onChange = vi.fn();
    render(<AdminNavigation active="users" onChange={onChange} />);
    expect(screen.getByRole('button', { name: '用户' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('button', { name: 'Runtime 配置' })).toBeTruthy();
    expect(screen.getByRole('button', { name: '营销 Skills' })).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Run 诊断' }));
    expect(onChange).toHaveBeenCalledWith('diagnostics');
  });
});
