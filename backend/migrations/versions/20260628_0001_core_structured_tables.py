"""create core structured task and radar tables

Revision ID: 20260628_0001
Revises:
Create Date: 2026-06-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260628_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _has_table("background_tasks"):
        op.create_table(
            "background_tasks",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("task_id", sa.String(length=64), nullable=False),
            sa.Column("kind", sa.String(length=64), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("error", sa.String(length=1000), nullable=False),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("result_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
    _create_index_if_missing("ix_background_tasks_task_id", "background_tasks", ["task_id"], unique=True)
    _create_index_if_missing("ix_background_tasks_kind", "background_tasks", ["kind"])
    _create_index_if_missing("ix_background_tasks_state", "background_tasks", ["state"])

    if not _has_table("ai_tasks"):
        op.create_table(
            "ai_tasks",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("task_id", sa.String(length=64), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("model", sa.String(length=128), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("prompt_summary", sa.String(length=2000), nullable=False),
            sa.Column("context_json", sa.JSON(), nullable=False),
            sa.Column("output_json", sa.JSON(), nullable=True),
            sa.Column("validation_json", sa.JSON(), nullable=True),
            sa.Column("error", sa.String(length=1000), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
    _create_index_if_missing("ix_ai_tasks_task_id", "ai_tasks", ["task_id"], unique=True)
    _create_index_if_missing("ix_ai_tasks_state", "ai_tasks", ["state"])

    if not _has_table("radar_scans"):
        op.create_table(
            "radar_scans",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("scan_id", sa.String(length=64), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("top50_count", sa.Integer(), nullable=False),
            sa.Column("top4_count", sa.Integer(), nullable=False),
            sa.Column("market_heat", sa.Integer(), nullable=False),
            sa.Column("alert_count", sa.Integer(), nullable=False),
            sa.Column("duration_ms", sa.Integer(), nullable=False),
            sa.Column("error", sa.String(length=1000), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    _create_index_if_missing("ix_radar_scans_scan_id", "radar_scans", ["scan_id"], unique=True)
    _create_index_if_missing("ix_radar_scans_state", "radar_scans", ["state"])

    if not _has_table("radar_candidates"):
        op.create_table(
            "radar_candidates",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("scan_id", sa.String(length=64), nullable=False),
            sa.Column("symbol", sa.String(length=32), nullable=False),
            sa.Column("base_asset", sa.String(length=32), nullable=False),
            sa.Column("rank", sa.Integer(), nullable=False),
            sa.Column("score", sa.Float(), nullable=False),
            sa.Column("direction", sa.String(length=32), nullable=False),
            sa.Column("stage", sa.String(length=128), nullable=False),
            sa.Column("trigger_mode", sa.String(length=128), nullable=False),
            sa.Column("price", sa.Float(), nullable=False),
            sa.Column("change_5m", sa.Float(), nullable=False),
            sa.Column("change_15m", sa.Float(), nullable=False),
            sa.Column("change_1h", sa.Float(), nullable=False),
            sa.Column("oi_change", sa.Float(), nullable=False),
            sa.Column("fund_confirm_count", sa.Integer(), nullable=False),
            sa.Column("fund_confirm_total", sa.Integer(), nullable=False),
            sa.Column("fake_breakout_risk", sa.String(length=32), nullable=False),
            sa.Column("ai_candidate", sa.Boolean(), nullable=False),
            sa.Column("market_structure_json", sa.JSON(), nullable=False),
            sa.Column("score_features_json", sa.JSON(), nullable=False),
            sa.Column("score_explain_json", sa.JSON(), nullable=False),
            sa.Column("raw_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    _create_index_if_missing("ix_radar_candidates_scan_id", "radar_candidates", ["scan_id"])
    _create_index_if_missing("ix_radar_candidates_symbol", "radar_candidates", ["symbol"])
    _create_index_if_missing("ix_radar_candidates_rank", "radar_candidates", ["rank"])
    _create_index_if_missing("ix_radar_candidates_direction", "radar_candidates", ["direction"])
    _create_index_if_missing("ix_radar_candidates_ai_candidate", "radar_candidates", ["ai_candidate"])


def downgrade() -> None:
    _drop_index_if_exists("ix_radar_candidates_ai_candidate", "radar_candidates")
    _drop_index_if_exists("ix_radar_candidates_direction", "radar_candidates")
    _drop_index_if_exists("ix_radar_candidates_rank", "radar_candidates")
    _drop_index_if_exists("ix_radar_candidates_symbol", "radar_candidates")
    _drop_index_if_exists("ix_radar_candidates_scan_id", "radar_candidates")
    _drop_table_if_exists("radar_candidates")

    _drop_index_if_exists("ix_radar_scans_state", "radar_scans")
    _drop_index_if_exists("ix_radar_scans_scan_id", "radar_scans")
    _drop_table_if_exists("radar_scans")

    _drop_index_if_exists("ix_ai_tasks_state", "ai_tasks")
    _drop_index_if_exists("ix_ai_tasks_task_id", "ai_tasks")
    _drop_table_if_exists("ai_tasks")

    _drop_index_if_exists("ix_background_tasks_state", "background_tasks")
    _drop_index_if_exists("ix_background_tasks_kind", "background_tasks")
    _drop_index_if_exists("ix_background_tasks_task_id", "background_tasks")
    _drop_table_if_exists("background_tasks")


def _has_table(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    indexes = sa.inspect(op.get_bind()).get_indexes(table_name)
    return any(index.get("name") == index_name for index in indexes)


def _create_index_if_missing(
    index_name: str,
    table_name: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    if _has_table(table_name) and not _has_index(table_name, index_name):
        op.create_index(index_name, table_name, columns, unique=unique)


def _drop_index_if_exists(index_name: str, table_name: str) -> None:
    if _has_index(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)


def _drop_table_if_exists(table_name: str) -> None:
    if _has_table(table_name):
        op.drop_table(table_name)
