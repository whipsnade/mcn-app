"""selection 包：KOL 圈选评分与归一化（纯函数，不依赖数据库）。"""

from app.selection.scoring_v2 import (
    SCORE_VERSION_V2,
    WEIGHTS_V2,
    ScoreContextV2,
    ScoreInputsV2,
    score_candidate_v2,
)
from app.selection.scoring_v3 import (
    EFFECT_WEIGHTS_V3,
    CandidateInputV3,
    CandidateScoreV3,
    DimensionScoreV3,
    ScoreContextV3,
    score_and_rank_candidates_v3,
)

__all__ = [
    "SCORE_VERSION_V2",
    "WEIGHTS_V2",
    "ScoreContextV2",
    "ScoreInputsV2",
    "score_candidate_v2",
    "EFFECT_WEIGHTS_V3",
    "CandidateInputV3",
    "CandidateScoreV3",
    "DimensionScoreV3",
    "ScoreContextV3",
    "score_and_rank_candidates_v3",
]
