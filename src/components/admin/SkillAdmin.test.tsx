import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  activateAdminSkill,
  createAdminSkillRevision,
  getAdminSkill,
  getAdminSkillDiff,
  listAdminSkills,
  rollbackAdminSkill,
  validateAdminSkill,
} from '../../api/skills';
import type { ApiSkillActivation, ApiSkillDetail, ApiSkillRevision } from '../../api/contracts';
import SkillAdmin from './SkillAdmin';

vi.mock('../../api/skills', () => ({
  activateAdminSkill: vi.fn(),
  createAdminSkillRevision: vi.fn(),
  getAdminSkill: vi.fn(),
  getAdminSkillDiff: vi.fn(),
  listAdminSkills: vi.fn(),
  rollbackAdminSkill: vi.fn(),
  validateAdminSkill: vi.fn(),
}));

const mockList = vi.mocked(listAdminSkills);
const mockDetail = vi.mocked(getAdminSkill);
const mockValidate = vi.mocked(validateAdminSkill);
const mockCreate = vi.mocked(createAdminSkillRevision);
const mockDiff = vi.mocked(getAdminSkillDiff);
const mockActivate = vi.mocked(activateAdminSkill);
const mockRollback = vi.mocked(rollbackAdminSkill);

const REVISION_1: ApiSkillRevision = {
  id: 'rev-1',
  tenant_id: null,
  scope_key: '__global__',
  skill_name: 'brand-research',
  revision: 1,
  content: '---\nname: brand-research\ndescription: 基线\nrequired_tools: []\n---\n基线正文',
  content_digest: 'sha256:one',
  description: '基线',
  required_tools: [],
  artifact_contract: 'brand_report_v3',
  created_by: 'admin-1',
  created_at: '2026-08-21T10:00:00Z',
  change_note: '初始版本',
};

const REVISION_2 = {
  ...REVISION_1,
  id: 'rev-2',
  revision: 2,
  content: '---\nname: brand-research\ndescription: 新版\nrequired_tools: []\n---\n新版正文',
  content_digest: 'sha256:two',
  description: '新版',
  created_at: '2026-08-21T11:00:00Z',
  change_note: '补充长尾说明',
};

const ACTIVE: ApiSkillActivation = {
  id: 'activation-1',
  environment: 'production',
  tenant_id: null,
  scope_key: '__global__',
  skill_name: 'brand-research',
  active_revision: 2,
  active_revision_id: 'rev-2',
  previous_revision: 1,
  previous_revision_id: 'rev-1',
  rollout_percent: 100,
  previous_rollout_percent: null,
  updated_by: 'admin-2',
  updated_at: '2026-08-21T11:05:00Z',
};

const DETAIL: ApiSkillDetail = {
  skill_name: 'brand-research',
  revisions: [REVISION_2, REVISION_1],
  activations: [ACTIVE],
};

function renderSkillAdmin() {
  return render(<SkillAdmin />);
}

describe('SkillAdmin', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockList.mockResolvedValue({
      items: [{ skill_name: 'brand-research', latest_revision: 2, revision_count: 2, active: [ACTIVE] }],
      total: 1,
    });
    mockDetail.mockResolvedValue(DETAIL);
    mockValidate.mockResolvedValue({
      valid: true,
      name: 'brand-research',
      description: '新版',
      required_tools: [],
      artifact_contract: 'brand_report_v3',
      content_digest: 'sha256:two',
      errors: [],
    });
    mockCreate.mockResolvedValue(REVISION_2);
    mockDiff.mockResolvedValue({
      skill_name: 'brand-research',
      from_revision: 1,
      to_revision: 2,
      diff: '-基线正文\n+新版正文',
    });
    mockActivate.mockResolvedValue(ACTIVE);
    mockRollback.mockResolvedValue({ ...ACTIVE, active_revision: 1, previous_revision: 2 });
  });

  it('loads revisions, shows active/audit fields, edits content, and creates a revision', async () => {
    renderSkillAdmin();

    expect(await screen.findByText('生产 / __global__ / 全量 · v2')).toBeTruthy();
    expect(screen.getByText(/更新人：admin-2/)).toBeTruthy();
    expect(screen.getAllByText('创建人：admin-1')).toHaveLength(2);

    const editor = screen.getByLabelText('Skill Markdown');
    fireEvent.change(editor, { target: { value: REVISION_2.content + '\n新增段落' } });
    fireEvent.change(screen.getByLabelText('变更说明'), { target: { value: '编辑器测试' } });
    fireEvent.click(screen.getByRole('button', { name: '保存新 Revision' }));

    await waitFor(() => {
      expect(mockCreate).toHaveBeenCalledWith(
        'brand-research',
        { content: REVISION_2.content + '\n新增段落', tenant_id: null, change_note: '编辑器测试' },
        expect.any(String),
      );
    });
  });

  it('reuses the same idempotency key after a lost create response', async () => {
    mockCreate.mockRejectedValueOnce(new Error('网络中断')).mockResolvedValue(REVISION_2);
    renderSkillAdmin();
    await screen.findByLabelText('Skill Markdown');

    fireEvent.click(screen.getByRole('button', { name: '保存新 Revision' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('网络中断');
    fireEvent.click(screen.getByRole('button', { name: '保存新 Revision' }));

    await waitFor(() => expect(mockCreate).toHaveBeenCalledTimes(2));
    expect(mockCreate.mock.calls[0]?.[2]).toBe(mockCreate.mock.calls[1]?.[2]);
  });

  it('shows structured validation errors and loads database diff', async () => {
    mockValidate.mockResolvedValueOnce({
      valid: false,
      name: null,
      description: null,
      required_tools: [],
      artifact_contract: null,
      content_digest: 'sha256:invalid',
      errors: [{ code: 'unknown_tool', message: '工具未审核', line: 4 }],
    });
    renderSkillAdmin();
    await screen.findByRole('button', { name: '校验当前内容' });

    fireEvent.click(screen.getByRole('button', { name: '校验当前内容' }));
    expect(await screen.findByText(/unknown_tool：工具未审核/)).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: '加载 Diff' }));
    expect(await screen.findByText(/-基线正文/)).toBeTruthy();
    expect(mockDiff).toHaveBeenCalledWith('brand-research', 1, 2, undefined, 'rev-1', 'rev-2');
  });

  it('activates with tenant rollout or full rollout and confirms rollback in dialog', async () => {
    renderSkillAdmin();
    await screen.findByLabelText('租户 ID');

    fireEvent.change(screen.getByLabelText('租户 ID'), { target: { value: 'tenant-1' } });
    fireEvent.change(screen.getByLabelText('灰度比例'), { target: { value: '25' } });
    mockDetail.mockResolvedValue({
      ...DETAIL,
      activations: [{ ...ACTIVE, tenant_id: 'tenant-1' }],
    });
    fireEvent.click(screen.getByRole('button', { name: '激活当前 Revision' }));
    await waitFor(() => {
      expect(mockActivate).toHaveBeenCalledWith(
        'brand-research',
        { revision: 2, revision_id: 'rev-2', tenant_id: 'tenant-1', environment: 'production', rollout_percent: 25 },
        expect.any(String),
      );
    });

    fireEvent.click(screen.getByRole('button', { name: '全量激活' }));
    await waitFor(() => {
      expect(mockActivate).toHaveBeenCalledWith(
        'brand-research',
        { revision: 2, revision_id: 'rev-2', tenant_id: 'tenant-1', environment: 'production', rollout_percent: 100 },
        expect.any(String),
      );
    });

    fireEvent.click(screen.getByRole('button', { name: '回滚上一 Revision' }));
    expect(screen.getByRole('dialog', { name: '回滚 Skill 激活' })).toBeTruthy();
    expect(mockRollback).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '确认回滚' }));
    await waitFor(() => {
      expect(mockRollback).toHaveBeenCalledWith(
        'brand-research',
        { tenant_id: 'tenant-1', environment: 'production' },
        expect.any(String),
      );
    });
  });
});
