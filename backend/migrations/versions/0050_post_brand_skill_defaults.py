"""post-brand skill defaults: add input contract version and insert revision 4.

Revision ID: 0050_post_brand_skill_defaults
Revises: 0049_skill_rollout_history
Create Date: 2026-08-22

- additive 为 skill_revisions 增加非空 model_input_contract_version（旧行
  server default 固定为 direct_model_input_v1）；
- 从 post-brand-default bundle 常量插入 social-marketing-analyst Revision 4
  （candidate，不激活）；
- 绝不 UPDATE content，也绝不 UPDATE/INSERT 任何 skill_activations 指针。
"""

from __future__ import annotations

import hashlib
import json
import uuid

import sqlalchemy as sa
from alembic import op

revision: str = "0050_post_brand_skill_defaults"
down_revision: str | None = "0049_skill_rollout_history"
branch_labels = None
depends_on = None

REV4_SKILL_NAME = "social-marketing-analyst"
REV4_REVISION = 4
REV4_SCOPE_KEY = "__global__"
REV4_CONTENT_DIGEST = "757e50e4ce6ae41dc0d286b84f5caa843f3bd7f26bb99241eee49f0606f5b922"
REV4_MODEL_INPUT_CONTRACT_VERSION = "direct_model_input_v1"
REV4_CHANGE_NOTE = "post-brand-default bundle candidate (Revision 4)"
REV4_CONTENT = "---\nname: social-marketing-analyst\ndescription: 处理社媒营销研究、报告、钻取与策略咨询，并按需加载专项 Skill。\nrequired_tools:\n  - load_marketing_skill\n  - request_clarification\n  - publish_artifacts\nartifact_contract: marketing_root_v1\nmodel_input_contract_version: direct_model_input_v1\n---\n\n# 社媒营销分析总则\n\n仅处理品牌、活动、达人和社媒营销语境的问题。遇到非营销主题，按根策略固定\n回复后结束。根据用户目标按需加载品牌报告、活动评估、达人圈选、产物钻取或\n策略咨询专项 Skill。\n\n完整报告需要明确对象、日期窗口、平台及问题；信息不足时调用\nrequest_clarification 提出一个最关键的问题，此后不得继续查询或发布。\n\n- 任何正式输出都必须经 build_artifact_draft 按 load_marketing_skill 返回的\n  model_input_contract 构造输入，再经 publish_artifacts 发布；不能手写正式\n  payload、Excel 或 BI。\n- 每次正式产物调用前先加载对应 Skill 获取 model_input_schema 与\n  concise_example；校验失败按结构化字段级错误修正。\n- DataTap 错误、空结果和超时如实保留，不以编造数据或替代口径冒充原始结果。\n\n## 外部数据获取止损纪律\n\n- 当同一业务目标的重复服务端失败已经表明继续微调参数不会增加可靠信息时，停止无效探测；保留已 settled 的结果，把受影响章节标记为 restricted 并写明 limitation。\n- 优先保证用户要求范围的真实覆盖，不为追求穷尽而反复调用同族能力。\n- 只有当已返回数据确实需要新的聚合口径时，才由模型自主决定是否另行重聚合；不得把重聚合作为固定阶段。\n"


def _normalized(content: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFC", content.replace("\r\n", "\n").replace("\r", "\n"))


def upgrade() -> None:
    op.add_column(
        "skill_revisions",
        sa.Column(
            "model_input_contract_version",
            sa.String(length=64),
            nullable=False,
            server_default="direct_model_input_v1",
        ),
    )
    digest = hashlib.sha256(_normalized(REV4_CONTENT).encode("utf-8")).hexdigest()
    if digest != REV4_CONTENT_DIGEST:
        raise RuntimeError("post_brand_revision4_digest_mismatch")
    conn = op.get_bind()
    existing = conn.execute(
        sa.text(
            "SELECT id, content_digest FROM skill_revisions "
            "WHERE scope_key = :scope AND skill_name = :name AND revision = :rev"
        ),
        {"scope": REV4_SCOPE_KEY, "name": REV4_SKILL_NAME, "rev": REV4_REVISION},
    ).fetchone()
    if existing is not None:
        if existing.content_digest != REV4_CONTENT_DIGEST:
            raise RuntimeError("post_brand_revision4_digest_conflict")
        return
    conn.execute(
        sa.text(
            "INSERT INTO skill_revisions "
            "(id, tenant_id, scope_key, skill_name, revision, content, content_digest, "
            "description, required_tools, artifact_contract, model_input_contract_version, "
            "created_by, created_at, change_note) "
            "VALUES (:id, NULL, :scope, :name, :rev, :content, :digest, :desc, :tools, "
            ":contract, :contract_version, NULL, NOW(), :note)"
        ),
        {
            "id": str(uuid.uuid4()),
            "scope": REV4_SCOPE_KEY,
            "name": REV4_SKILL_NAME,
            "rev": REV4_REVISION,
            "content": REV4_CONTENT,
            "digest": REV4_CONTENT_DIGEST,
            "desc": "处理社媒营销研究、报告、钻取与策略咨询，并按需加载专项 Skill。",
            "tools": json.dumps(
                ["load_marketing_skill", "request_clarification", "publish_artifacts"]
            ),
            "contract": "marketing_root_v1",
            "contract_version": REV4_MODEL_INPUT_CONTRACT_VERSION,
            "note": REV4_CHANGE_NOTE,
        },
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM skill_revisions WHERE scope_key = :scope AND skill_name = :name "
            "AND revision = :rev AND content_digest = :digest"
        ).bindparams(
            scope=REV4_SCOPE_KEY,
            name=REV4_SKILL_NAME,
            rev=REV4_REVISION,
            digest=REV4_CONTENT_DIGEST,
        )
    )
    op.drop_column("skill_revisions", "model_input_contract_version")
