"""Strongly-typed analysis artifact payloads (spec §12.1/§12.2).

Every business artifact the model produces must validate against one of the
exported payloads before it can be reviewed / published. `TYPED_PAYLOAD_BY_SCHEMA`
maps a fixed `schema_version` to its payload type for dispatch by later tasks
(validation, lineage, BI, exporters).
"""

from __future__ import annotations

from app.agent_artifacts.payloads.brand import BrandReportV3
from app.agent_artifacts.payloads.campaign import CampaignReportV2, CampaignReportV3
from app.agent_artifacts.payloads.common import (
    ArtifactPayloadBase,
    Limitation,
    Methodology,
    SectionAvailability,
)
from app.agent_artifacts.payloads.insight import InsightBoardV1
from app.agent_artifacts.payloads.kol_analysis import KolAnalysisV2
from app.agent_artifacts.payloads.kol_detail import KolDetailV2
from app.agent_artifacts.payloads.kol_selection import KolSelectionScopeV3, KolSelectionV3

TYPED_PAYLOAD_BY_SCHEMA: dict[str, type[ArtifactPayloadBase]] = {
    "brand_report_v3": BrandReportV3,
    "campaign_report_v2": CampaignReportV2,
    "campaign_report_v3": CampaignReportV3,
    "kol_selection_v3": KolSelectionV3,
    "kol_analysis_v2": KolAnalysisV2,
    "kol_detail_v2": KolDetailV2,
    "insight_board_v1": InsightBoardV1,
}

__all__ = [
    "TYPED_PAYLOAD_BY_SCHEMA",
    "ArtifactPayloadBase",
    "BrandReportV3",
    "CampaignReportV2",
    "CampaignReportV3",
    "InsightBoardV1",
    "KolAnalysisV2",
    "KolDetailV2",
    "KolSelectionScopeV3",
    "KolSelectionV3",
    "Limitation",
    "Methodology",
    "SectionAvailability",
]
