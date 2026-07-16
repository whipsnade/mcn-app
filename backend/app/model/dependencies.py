from functools import lru_cache

from app.core.config import get_settings
from app.model.adapter import ModelAdapter
from app.model.tencent_plan import TencentPlanAdapter


@lru_cache
def get_model_adapter() -> ModelAdapter:
    return TencentPlanAdapter.from_settings(get_settings())
