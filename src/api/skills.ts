import { request } from './client';
import type {
  AdminSkillEnvironment,
  ApiSkillActivation,
  ApiSkillDetail,
  ApiSkillDiff,
  ApiSkillList,
  ApiSkillRevision,
  ApiSkillValidation,
} from './contracts';

const idempotencyHeaders = (value?: string): Record<string, string> => ({
  'Idempotency-Key': value ?? crypto.randomUUID(),
});

const encodeSkillName = (skillName: string): string => encodeURIComponent(skillName);

export interface AdminSkillValidationInput {
  expected_name?: string | null;
  content: string;
}

export interface AdminSkillRevisionInput {
  content: string;
  tenant_id?: string | null;
  change_note?: string | null;
}

export interface AdminSkillActivationInput {
  revision: number;
  revision_id?: string;
  tenant_id?: string | null;
  environment?: AdminSkillEnvironment;
  rollout_percent?: number;
}

export interface AdminSkillRollbackInput {
  tenant_id?: string | null;
  environment?: AdminSkillEnvironment;
}

export const listAdminSkills = (): Promise<ApiSkillList> =>
  request<ApiSkillList>('/api/v1/admin/skills');

export const getAdminSkill = (skillName: string): Promise<ApiSkillDetail> =>
  request<ApiSkillDetail>(`/api/v1/admin/skills/${encodeSkillName(skillName)}`);

export const getAdminSkillRevision = (
  skillName: string,
  revision: number,
): Promise<ApiSkillRevision> =>
  request<ApiSkillRevision>(
    `/api/v1/admin/skills/${encodeSkillName(skillName)}/revisions/${revision}`,
  );

export const validateAdminSkill = (
  input: AdminSkillValidationInput,
): Promise<ApiSkillValidation> =>
  request<ApiSkillValidation>('/api/v1/admin/skills/validate', {
    method: 'POST',
    body: JSON.stringify(input),
  });

export const createAdminSkillRevision = (
  skillName: string,
  input: AdminSkillRevisionInput,
  idempotencyKey?: string,
): Promise<ApiSkillRevision> =>
  request<ApiSkillRevision>(
    `/api/v1/admin/skills/${encodeSkillName(skillName)}/revisions`,
    {
      method: 'POST',
      headers: idempotencyHeaders(idempotencyKey),
      body: JSON.stringify(input),
    },
  );

export const getAdminSkillDiff = (
  skillName: string,
  fromRevision: number,
  toRevision: number,
  tenantId?: string | null,
  fromRevisionId?: string,
  toRevisionId?: string,
): Promise<ApiSkillDiff> =>
  request<ApiSkillDiff>(
    `/api/v1/admin/skills/${encodeSkillName(skillName)}/diff?from_revision=${fromRevision}&to_revision=${toRevision}${tenantId ? `&tenant_id=${encodeURIComponent(tenantId)}` : ''}${fromRevisionId ? `&from_revision_id=${encodeURIComponent(fromRevisionId)}` : ''}${toRevisionId ? `&to_revision_id=${encodeURIComponent(toRevisionId)}` : ''}`,
  );

export const activateAdminSkill = (
  skillName: string,
  input: AdminSkillActivationInput,
  idempotencyKey?: string,
): Promise<ApiSkillActivation> =>
  request<ApiSkillActivation>(
    `/api/v1/admin/skills/${encodeSkillName(skillName)}/activate`,
    {
      method: 'POST',
      headers: idempotencyHeaders(idempotencyKey),
      body: JSON.stringify(input),
    },
  );

export const rollbackAdminSkill = (
  skillName: string,
  input: AdminSkillRollbackInput,
  idempotencyKey?: string,
): Promise<ApiSkillActivation> =>
  request<ApiSkillActivation>(
    `/api/v1/admin/skills/${encodeSkillName(skillName)}/rollback`,
    {
      method: 'POST',
      headers: idempotencyHeaders(idempotencyKey),
      body: JSON.stringify(input),
    },
  );
