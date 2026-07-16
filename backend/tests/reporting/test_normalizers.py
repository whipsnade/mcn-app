from datetime import datetime, timezone
import json

from app.reporting.normalizers import normalize_tool_evidence
from app.reporting.schemas import ToolEvidence


def test_datatap_candidate_exports_template_fields_without_raw_urls() -> None:
    evidence = ToolEvidence(
        internal_tool_name="datatap.xiaohongshu.kol.search.v1",
        source_call_id="call-1",
        collected_at=datetime.now(timezone.utc),
        payload={
            "result": json.dumps(
                {
                    "KOL 列表": [
                        {
                            "账号ID (kwUid)": "uid-1",
                            "昵称": "测试达人",
                            "粉丝数": "2.5万",
                            "城市": "浙江",
                            "总获赞": 12345,
                            "内容标签": ["护肤", "测评"],
                            "主页": "https://example.test/profile/uid-1",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        },
    )

    [candidate] = normalize_tool_evidence([evidence])

    assert candidate.export_fields == {
        "city": "浙江",
        "total_likes": 12345,
        "content_tags": ["护肤", "测评"],
    }
    assert "主页" not in candidate.export_fields
    assert "export_fields" in candidate.as_dict()
