"""SQLite persistence layer — the v3 system of record (spec §4)."""

from auto_applier.db.engine import (
    backup_db,
    connect,
    init_app_db,
    rotate_backups,
    tx,
)
from auto_applier.db.repositories import (
    AnswerRepo,
    ApplicationRepo,
    JobRepo,
    OutcomeRepo,
    ScoreRepo,
    SkillGapRepo,
)

__all__ = [
    "AnswerRepo",
    "ApplicationRepo",
    "JobRepo",
    "OutcomeRepo",
    "ScoreRepo",
    "SkillGapRepo",
    "backup_db",
    "connect",
    "init_app_db",
    "rotate_backups",
    "tx",
]
