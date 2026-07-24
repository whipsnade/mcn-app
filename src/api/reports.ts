import { request } from './client';
import type {
  ApiArtifactsSummary,
  ApiSessionReportItem,
  ArtifactModuleKey,
} from './contracts';


export type SessionReportType = 'brand_analysis' | 'campaign_analysis' | 'kol_analysis';

export function listSessionReports(
  sessionId: string,
  reportType: SessionReportType,
): Promise<ApiSessionReportItem[]> {
  return request<ApiSessionReportItem[]>(
    `/api/v1/sessions/${sessionId}/reports?report_type=${reportType}`,
  );
}

export function getArtifactsSummary(sessionId: string): Promise<ApiArtifactsSummary> {
  return request<ApiArtifactsSummary>(`/api/v1/sessions/${sessionId}/artifacts/summary`);
}

export function markArtifactRead(
  sessionId: string,
  moduleKey: ArtifactModuleKey,
  artifactId: string,
): Promise<void> {
  return request<void>(`/api/v1/sessions/${sessionId}/artifact-read-state`, {
    method: 'PUT',
    body: JSON.stringify({ module_key: moduleKey, artifact_id: artifactId }),
  });
}
