"""Message ORM → API DTO 序列化（独立模块，避免 router 间循环导入）。"""

from app.workspace.models import Message
from app.workspace.schemas import MessageRead


def message_read(message: Message) -> MessageRead:
    return MessageRead(
        id=message.id,
        role=message.role,
        content=message.content,
        sequence=message.sequence,
        metadata=public_message_metadata(message.metadata_json),
        created_at=message.created_at,
    )


def public_message_metadata(metadata: dict) -> dict:
    """Expose only UI metadata; never return internal locks or raw provider data."""
    allowed = {
        "task_id",
        "status",
        "analysis_task_ids",
        "latest_analysis_task_id",
        "followup_suggestions_status",
        "followup_suggestions",
        "followup_suggestions_generated_at",
        "followup_suggestions_started_at",
        "followup_error",
        "brainstorm",
        "clarify",
        "turn_id",
        "thinking",
    }
    return {key: value for key, value in metadata.items() if key in allowed}
