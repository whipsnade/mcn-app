from functools import lru_cache

from app.core.config import get_settings
from app.model.adapter import ModelAdapter
from app.model.fake import FakeModelAdapter
from app.model.tencent_plan import TencentPlanAdapter


@lru_cache
def get_model_adapter() -> ModelAdapter:
    settings = get_settings()
    if settings.model_provider == "fake":
        return FakeModelAdapter()
    return TencentPlanAdapter.from_settings(settings)
