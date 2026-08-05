"""统一模型动作协议（设计文档 §六）。

模型每轮决策必须输出且仅输出下列四种动作之一，由 ``action`` 字段判别
（strict discriminated union）。所有动作一律 ``extra="forbid"``：未知字段
被视为非法输出，引擎据此重试而不是静默吞掉。

Artifact 创建、更新、历史读取和计算统一通过 ``call_tool`` 走受控内部工具，
不在顶层动作中增加业务特例。

直接发布协议：``publish_artifacts`` 取代旧的 ``submit_review``——不再创建
Reviewer Run，由确定性发布服务逐 Draft 校验并发布；``publish_artifacts``
是非终态动作，发布结果回喂后模型继续决策循环。``SubmitReview`` 类仅保留
供历史代码/测试导入，不在动作联合中。
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

# 四种动作的权威集合；Profile 的 allowed_actions 必须是它的子集。
FOUR_ACTIONS: frozenset[str] = frozenset({"ask_user", "call_tool", "publish_artifacts", "complete"})


class AskUser(BaseModel):
    """向用户提出澄清问题；本 Run 以 ``clarification_requested`` 结果完成。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["ask_user"]
    question: str = Field(min_length=1, max_length=1000)
    # 可空；当提供选项时必须是 2-4 项。
    options: list[str] | None = None

    @field_validator("options")
    @classmethod
    def _options_length(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and not 2 <= len(value) <= 4:
            raise ValueError("options must contain 2-4 items when present")
        return value


class CallTool(BaseModel):
    """调用受控内部工具（已审核 MCP、历史、计算、Artifact Draft）。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["call_tool"]
    internal_tool_name: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any]
    rationale: str = Field(min_length=1, max_length=2000)


class PublishArtifacts(BaseModel):
    """把本 Run 已构建完成的正式 Draft 直接发布给用户（非终态动作）。

    发布由确定性发布服务逐 Draft 校验执行，无模型 Reviewer；逐项结果
    （published / validation_failed / failed + 结构化错误）回喂后模型继续
    决策循环：修订后重新发布，或用 abandon_draft 工具放弃无法修复的 Draft。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["publish_artifacts"]
    artifact_draft_ids: tuple[Annotated[str, Field(min_length=1)], ...] = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=2000)


class SubmitReview(BaseModel):
    """（遗留）提交 Draft 给 Reviewer 的旧动作模型。

    已从动作联合与 ``FOUR_ACTIONS`` 移除：新执行路径不再创建 Reviewer Run。
    类定义仅保留供历史代码/测试导入，引擎 rewiring（Task 4）完成后可删除。

    ``artifact_draft_ids`` 用 tuple 而非 list：不可变且天然带去重语义，
    配合 validator 保证非空且唯一，且每项为至少 1 字符的非空字符串。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["submit_review"]
    artifact_draft_ids: tuple[Annotated[str, Field(min_length=1)], ...] = Field(min_length=1)
    # Reviewer 全部 approve 并原子发布后才写入 assistant 消息。
    completion_text: str = Field(min_length=1, max_length=4000)
    summary: str = Field(min_length=1, max_length=2000)

    @field_validator("artifact_draft_ids")
    @classmethod
    def _unique_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("artifact_draft_ids must not contain duplicates")
        return value


class Complete(BaseModel):
    """无正式 Artifact 时结束 Run；回复用户文本，可附可选后续建议。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["complete"]
    text: str = Field(min_length=1, max_length=4000)
    suggestions: list[str] | None = None


AgentAction = Annotated[
    AskUser | CallTool | PublishArtifacts | Complete,
    Field(discriminator="action"),
]

# 裸 Annotated 联合没有 model_validate，解析模型输出一律走该适配器。
AGENT_ACTION_ADAPTER: TypeAdapter[AgentAction] = TypeAdapter(AgentAction)


__all__ = [
    "AGENT_ACTION_ADAPTER",
    "AgentAction",
    "AskUser",
    "CallTool",
    "Complete",
    "FOUR_ACTIONS",
    "PublishArtifacts",
    "SubmitReview",
]
