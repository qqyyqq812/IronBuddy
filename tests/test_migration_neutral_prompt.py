"""V7.30 R2: SQL migration sanity checks (does not execute against live DB)."""
import os
import re

MIGRATION = os.path.join(
    os.path.dirname(__file__), "..", "migrations",
    "2026-04-26-neutral-system-prompt.sql",
)


def _read():
    with open(MIGRATION, "r", encoding="utf-8") as f:
        return f.read()


def test_migration_file_exists():
    assert os.path.exists(MIGRATION)


def test_uses_transaction():
    sql = _read()
    assert "BEGIN TRANSACTION" in sql
    assert "COMMIT" in sql


def test_inserts_active_row():
    sql = _read()
    assert "INSERT INTO system_prompt_versions" in sql
    insert_block = re.search(r"INSERT INTO[\s\S]+?\);", sql).group(0)
    # active=1 in the VALUES tuple (the 4th positional value)
    assert "1," in insert_block
    assert "datetime('now')" in insert_block


def test_demotes_other_actives_after_insert():
    sql = _read()
    assert "UPDATE system_prompt_versions" in sql
    update_block = re.search(r"UPDATE system_prompt_versions[\s\S]+?;", sql).group(0)
    assert "SET active = 0" in update_block
    assert "MAX(id)" in update_block


def _strip_sql_comments(sql):
    return "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))


def test_neutral_prompt_avoids_biased_phrases():
    code = _strip_sql_comments(_read())
    # R2 fix: new prompt body must NOT bake in user-specific preferences
    forbidden = ["偏好肌群", "疲劳容忍", "膝盖", "knee_caution"]
    for phrase in forbidden:
        assert phrase not in code, "neutral prompt must not include %r" % phrase


def test_prompt_includes_command_redirect_phrasing():
    sql = _read()
    assert "切到深蹲" in sql
    assert "回答简短" in sql or "3 句话以内" in sql
