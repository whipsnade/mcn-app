from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.workspace.schemas import MessageRead, Platform


class BrainstormPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = Field(min_length=1, max_length=10)
    end: str = Field(min_length=1, max_length=10)


class BrainstormProfile(BaseModel):
    """澄清画像：未确认的字段保持 null（platforms 保持空数组）。"""

    model_config = ConfigDict(extra="forbid")

    brand: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=100)
    platforms: list[Platform] = Field(default_factory=list)
    audience: str | None = Field(default=None, max_length=500)
    period: BrainstormPeriod | None = None
    kol_filters: str | None = Field(default=None, max_length=500)
    goal: str | None = Field(default=None, max_length=500)
    region: str | None = Field(default=None, max_length=200)


class BrainstormQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500)
    options: list[str] = Field(default_factory=list, max_length=4)


class BrainstormModelOutput(BaseModel):
    """BRAINSTORM 模型的单轮澄清输出。"""

    model_config = ConfigDict(extra="forbid")

    ready: bool
    assistant_message: str = Field(min_length=1, max_length=2000)
    question: BrainstormQuestion | None = None
    extracted: BrainstormProfile = Field(default_factory=BrainstormProfile)
    # 提炼不出合适标题时输出空字符串，后端保留「新会话N」默认名。
    title_suggestion: str = Field(default="", max_length=20)


class BrainstormRequest(BaseModel):
    content: str = Field(min_length=1, max_length=5000)


class BrainstormOutcome(BaseModel):
    """service 内部结果：router 据此提交事务并决定是否启动任务。"""

    ready: bool
    task_id: str | None
    message: MessageRead
    profile: BrainstormProfile


class BrainstormResponse(BaseModel):
    ready: bool
    task_id: str | None = None
    message: MessageRead
    profile: BrainstormProfile


def merge_profile(current: BrainstormProfile, extracted: BrainstormProfile) -> BrainstormProfile:
    """以本轮提炼覆盖画像：模型只确认过的字段非空，空值不擦除已有确认。"""

    updates: dict[str, Any] = {}
    for field in BrainstormProfile.model_fields:
        value = getattr(extracted, field)
        if value is None or value == []:
            continue
        updates[field] = value
    return current.model_copy(update=updates)
