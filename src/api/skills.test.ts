import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  activateAdminSkill,
  createAdminSkillRevision,
  getAdminSkill,
  getAdminSkillDiff,
  getAdminSkillRevision,
  listAdminSkills,
  rollbackAdminSkill,
  validateAdminSkill,
} from './skills';
import { request } from './client';

vi.mock('./client', () => ({
  request: vi.fn(),
}));

const mockRequest = vi.mocked(request);
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function lastHeaders(): Record<string, string> {
  const init = mockRequest.mock.calls.at(-1)?.[1] as { headers?: Record<string, string> } | undefined;
  return init?.headers ?? {};
}

describe('marketing skills admin api', () => {
  beforeEach(() => {
    mockRequest.mockReset();
    mockRequest.mockResolvedValue(undefined as never);
  });

  it('lists skills and loads detail/revision with encoded names', async () => {
    await listAdminSkills();
    expect(mockRequest).toHaveBeenCalledWith('/api/v1/admin/skills');

    await getAdminSkill('brand research');
    expect(mockRequest).toHaveBeenCalledWith('/api/v1/admin/skills/brand%20research');

    await getAdminSkillRevision('brand research', 2);
    expect(mockRequest).toHaveBeenCalledWith('/api/v1/admin/skills/brand%20research/revisions/2');
  });

  it('validates content without an idempotency header', async () => {
    await validateAdminSkill({ expected_name: 'brand-research', content: '# draft' });
    expect(mockRequest).toHaveBeenCalledWith('/api/v1/admin/skills/validate', {
      method: 'POST',
      body: JSON.stringify({ expected_name: 'brand-research', content: '# draft' }),
    });
  });

  it('creates revisions with an automatic or explicit idempotency key', async () => {
    await createAdminSkillRevision('brand-research', {
      content: '# draft',
      tenant_id: null,
      change_note: '补充长尾说明',
    });
    expect(mockRequest).toHaveBeenCalledWith(
      '/api/v1/admin/skills/brand-research/revisions',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ content: '# draft', tenant_id: null, change_note: '补充长尾说明' }),
      }),
    );
    expect(lastHeaders()['Idempotency-Key']).toMatch(UUID_PATTERN);

    await createAdminSkillRevision('brand-research', { content: '# v2' }, 'skill-revision-2');
    expect(lastHeaders()['Idempotency-Key']).toBe('skill-revision-2');
  });

  it('loads diff and sends tenant rollout activation including the idempotency key', async () => {
    await getAdminSkillDiff('brand-research', 1, 2);
    expect(mockRequest).toHaveBeenCalledWith(
      '/api/v1/admin/skills/brand-research/diff?from_revision=1&to_revision=2',
    );

    await activateAdminSkill(
      'brand-research',
      { revision: 2, tenant_id: 'tenant/one', environment: 'production', rollout_percent: 25 },
      'skill-activate-1',
    );
    expect(mockRequest).toHaveBeenCalledWith('/api/v1/admin/skills/brand-research/activate', {
      method: 'POST',
      headers: { 'Idempotency-Key': 'skill-activate-1' },
      body: JSON.stringify({
        revision: 2,
        tenant_id: 'tenant/one',
        environment: 'production',
        rollout_percent: 25,
      }),
    });
  });

  it('activates globally at 100 percent and rolls back with explicit idempotency', async () => {
    await activateAdminSkill('brand-research', {
      revision: 2,
      tenant_id: null,
      environment: 'staging',
      rollout_percent: 100,
    });
    expect(lastHeaders()['Idempotency-Key']).toMatch(UUID_PATTERN);

    await rollbackAdminSkill(
      'brand-research',
      { tenant_id: null, environment: 'staging' },
      'skill-rollback-1',
    );
    expect(mockRequest).toHaveBeenCalledWith('/api/v1/admin/skills/brand-research/rollback', {
      method: 'POST',
      headers: { 'Idempotency-Key': 'skill-rollback-1' },
      body: JSON.stringify({ tenant_id: null, environment: 'staging' }),
    });
  });
});
