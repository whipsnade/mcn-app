"""用户行业画像描述：把 industries 字段翻译成模型能理解的业务视角。

代码只提供"用户是谁"的背景，不规定模型走哪条工具链路——路径选择
（最快 vs 最准的权衡）完全交给模型基于画像自行判断。
"""

from __future__ import annotations

# 行业词 → 用户画像描述。未覆盖的行业走通用模板。
_RESTAURANT_PERSONA = (
    "用户是线下餐饮门店的运营人员，关注：同行餐饮门店与菜品内容、探店与"
    "到店引流类营销、本地消费趋势与食客口碑；判断内容相关性时以餐饮经营"
    "视角为准（如「餐厅」品类、到店场景），而非泛美食娱乐内容。"
)

_PERSONA_BY_INDUSTRY: dict[str, str] = {
    "美食": _RESTAURANT_PERSONA,
    "餐饮": _RESTAURANT_PERSONA,
}


def describe_user_persona(industries: list[str]) -> str:
    """把行业属性列表翻译为一段用户画像描述（注入 prompt 上下文）。"""
    parts = [
        _PERSONA_BY_INDUSTRY[industry]
        for industry in industries
        if industry in _PERSONA_BY_INDUSTRY
    ]
    if parts:
        return "；".join(dict.fromkeys(parts))
    label = "、".join(industries) if industries else "综合"
    return f"用户是{label}行业的运营人员，关注本行业的营销与内容动态。"


__all__ = ["describe_user_persona"]
