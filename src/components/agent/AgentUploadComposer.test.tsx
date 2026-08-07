import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import AgentUploadComposer from './AgentUploadComposer';

describe('AgentUploadComposer', () => {
  it('only accepts CSV/XLSX and exposes parsed/failed upload states', () => {
    const onUpload = vi.fn();
    render(
      <AgentUploadComposer
        uploads={[
          { id: 'parsed', original_filename: '投放.csv', mime_type: 'text/csv', size_bytes: 1, sha256: '', status: 'parsed', error_code: null, created_at: '', completed_at: null },
          { id: 'failed', original_filename: '说明.txt', mime_type: 'text/plain', size_bytes: 1, sha256: '', status: 'failed', error_code: 'unsupported', created_at: '', completed_at: null },
        ]}
        onUpload={onUpload}
        onRemove={vi.fn()}
      />,
    );
    expect(screen.getByText(/投放.csv · 已解析/)).toBeVisible();
    expect(screen.getByText(/说明.txt · 失败/)).toBeVisible();

    const input = screen.getByLabelText('上传资料', { selector: 'input' });
    fireEvent.change(input, { target: { files: [new File(['x'], '说明.txt', { type: 'text/plain' })] } });
    expect(onUpload).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { files: [new File(['x'], '投放.xlsx')] } });
    expect(onUpload).toHaveBeenCalledWith(expect.objectContaining({ name: '投放.xlsx' }));
  });
});
