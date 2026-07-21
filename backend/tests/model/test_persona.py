from app.model.persona import describe_user_persona


def test_restaurant_persona_for_food_industry() -> None:
    persona = describe_user_persona(["美食"])

    assert "线下餐饮门店" in persona
    assert "餐厅" in persona
    assert "泛美食娱乐" in persona


def test_persona_dedupes_and_falls_back_to_generic() -> None:
    assert describe_user_persona(["美食", "餐饮"]).count("线下餐饮门店") == 1
    assert describe_user_persona(["美妆"]) == "用户是美妆行业的运营人员，关注本行业的营销与内容动态。"
    assert describe_user_persona([]) == "用户是综合行业的运营人员，关注本行业的营销与内容动态。"
