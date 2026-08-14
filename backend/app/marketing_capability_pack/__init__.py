"""版本化、无基础设施依赖的营销业务能力包。"""

from .loader import CapabilityPackError, CapabilityPackLoader
from .models import CapabilityPackSnapshot, LoadedMarketingSkill

__all__ = [
    "CapabilityPackError",
    "CapabilityPackLoader",
    "CapabilityPackSnapshot",
    "LoadedMarketingSkill",
]
