import { describe, expect, it } from 'vitest';

import { safeHttpUrl } from './urlUtils';

describe('safeHttpUrl', () => {
  it('允许 http/https 绝对 URL 并返回规范化形式', () => {
    expect(safeHttpUrl('https://xhs.com/p1')).toBe('https://xhs.com/p1');
    expect(safeHttpUrl('http://example.com/a?b=1#c')).toBe('http://example.com/a?b=1#c');
    expect(safeHttpUrl('HTTPS://EXAMPLE.COM/X')).toBe('https://example.com/X');
  });

  it('拒绝 javascript:/data:/file: 等非 http 协议', () => {
    expect(safeHttpUrl('javascript:alert(1)')).toBeNull();
    expect(safeHttpUrl('JavaScript:alert(1)')).toBeNull();
    expect(safeHttpUrl('data:text/html,<script>alert(1)</script>')).toBeNull();
    expect(safeHttpUrl('file:///etc/passwd')).toBeNull();
    expect(safeHttpUrl('ftp://example.com/x')).toBeNull();
  });

  it('拒绝空值、相对路径与非法串', () => {
    expect(safeHttpUrl(undefined)).toBeNull();
    expect(safeHttpUrl(null)).toBeNull();
    expect(safeHttpUrl('')).toBeNull();
    expect(safeHttpUrl('/relative/path')).toBeNull();
    expect(safeHttpUrl('not a url')).toBeNull();
    expect(safeHttpUrl('xhs.com/p1')).toBeNull();
  });
});
