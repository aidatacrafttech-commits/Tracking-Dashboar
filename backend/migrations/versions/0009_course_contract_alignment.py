"""course contract alignment with AdhyaapakAI: language codes, no student names

* Lecture languages move from ISO-style codes (en, hi, ...) to the AdhyaapakAI app's codes
  (english, hindi, ...). Bhojpuri, Urdu and Assamese have no AdhyaapakAI equivalent: if any
  lecture uses them the upgrade stops instead of guessing.
* course_enrollments.student_name is dropped: enrollments keep only the app's opaque user id,
  so purchase history identifies nobody once the student's account is deleted.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-02 18:10:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LANGUAGE_MAP = {
    "en": "english", "hi": "hindi", "mr": "marathi", "ta": "tamil", "te": "telugu",
    "bn": "bengali", "gu": "gujarati", "kn": "kannada", "ml": "malayalam", "pa": "punjabi",
    "or": "odia",
}
UNMAPPED = ("bho", "ur", "as")


def _remap(mapping: dict[str, str]) -> None:
    bind = op.get_bind()
    for old, new in mapping.items():
        bind.execute(sa.text("UPDATE course_lecture_variants SET language = :new WHERE language = :old"),
                     {"old": old, "new": new})
        bind.execute(sa.text("UPDATE course_lectures SET default_language = :new WHERE default_language = :old"),
                     {"old": old, "new": new})


def upgrade() -> None:
    bind = op.get_bind()
    stuck = bind.execute(sa.text(
        "SELECT (SELECT COUNT(*) FROM course_lecture_variants WHERE language IN (:a, :b, :c))"
        " + (SELECT COUNT(*) FROM course_lectures WHERE default_language IN (:a, :b, :c))"
    ), dict(zip(("a", "b", "c"), UNMAPPED, strict=True))).scalar()
    if stuck:
        raise RuntimeError(
            f"{stuck} lecture(s) use Bhojpuri, Urdu or Assamese, which the AdhyaapakAI app does not "
            "support. Remove those language versions in the dashboard, then run the upgrade again."
        )
    _remap(LANGUAGE_MAP)
    with op.batch_alter_table('course_enrollments', schema=None) as batch_op:
        batch_op.drop_column('student_name')


def downgrade() -> None:
    with op.batch_alter_table('course_enrollments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('student_name', sa.String(length=120), nullable=True))
    # "hinglish" has no older code and is left as is (nothing is deleted on downgrade).
    _remap({new: old for old, new in LANGUAGE_MAP.items()})
