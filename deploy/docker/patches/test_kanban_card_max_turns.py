"""Unit tests for the per-card turn-budget patch (gke-labs/kube-agents#1947).

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

Each ``UPSTREAM_*`` holds the genuine region of its file at the pinned Hermes,
copied verbatim, inside enough scaffolding to compile (patchlib compiles a
file before writing it), so every anchor is exercised against the text it was
derived from. ``verify_kanban_card_max_turns.py`` runs the real modules in the
image build; here the dispatcher excerpt is executed with a stub ``Task``.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from apply_kanban_card_max_turns import (
    BUILD_MARKER, CONNECT_RELATIVE, DB_RELATIVE, DISPATCH_RELATIVE, SCHEMAS_RELATIVE,
    TOOLS_RELATIVE, apply,
)

UPSTREAM_CONNECT = '''_EARLY_TASK_COLUMNS = (
    # Ralph-style goal loop toggle; 0 = classic single-shot worker.
    ("goal_mode", "goal_mode INTEGER NOT NULL DEFAULT 0"),
    ("goal_max_turns", "goal_max_turns INTEGER"),
    ("completion_contract", "completion_contract TEXT"),
)
'''

UPSTREAM_DB = '''from typing import Iterable, Optional


class Task:
    goal_mode: bool = False
    goal_max_turns: Optional[int] = None
    session_id: Optional[str] = None         # originating HERMES_SESSION_ID; NULL from CLI/dashboard


# Text columns where "" is stored/read as "not set".
_TASK_EMPTY_IS_NULL_COLUMNS = (
    "model_override", "provider_override", "reasoning_effort", "goal_max_turns", "block_kind",
)

SCHEMA_SQL = """
    goal_mode            INTEGER NOT NULL DEFAULT 0,
    -- Goal-loop turn budget for ``goal_mode`` workers. NULL = use the
    -- goals-engine default.
    goal_max_turns       INTEGER,
"""


def create_task(
    conn, *, title: str, body: Optional[str] = None,
    provider_override: Optional[str] = None, reasoning_effort: Optional[str] = None,
    goal_mode: bool = False, goal_max_turns: Optional[int] = None, initial_status: str = "running",
    session_id: Optional[str] = None, board: Optional[str] = None, project_id: Optional[str] = None,
):
    conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, body, assignee, status, priority,
                        created_by, created_at, workspace_kind, workspace_path,
                        branch_name, project_id, tenant, idempotency_key,
                        max_runtime_seconds,
                        skills, max_retries, model_override, provider_override,
                        reasoning_effort,
                        goal_mode, goal_max_turns, session_id, completion_contract
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id, title.strip(), body, assignee, task_status, priority,
                        created_by, now, workspace_kind, workspace_path,
                        branch_name, project_id, tenant, idempotency_key,
                        _opt_int(max_runtime_seconds),
                        json.dumps(skills_list) if skills_list is not None else None,
                        _opt_int(max_retries), model_override, provider_override, reasoning_effort,
                        1 if goal_mode else 0, _opt_int(goal_max_turns), session_id, completion_contract,
                    ),
                )
'''

UPSTREAM_SCHEMAS = '''KANBAN_CREATE_PROPERTIES = {
        "goal_max_turns": _prop("integer", (
                "Turn budget for goal_mode workers. Caps how many "
                "continuation turns the worker may take before the task "
                "is blocked for review. Ignored unless goal_mode is "
                "true. Defaults to the goal-engine default (20)."
        )),
}
'''

UPSTREAM_TOOLS = '''def _handle_create(args, kb, conn, model_override, provider_override, goal_mode):
    return kb.create_task(
            conn,
            model_override=model_override, provider_override=provider_override,
            goal_mode=goal_mode, goal_max_turns=_opt_int(args.get("goal_max_turns")),
            completion_contract=args.get("completion_contract"),
    )
'''

UPSTREAM_DISPATCH = '''def _worker_argv(task, profile_arg, hermes_home):
    cmd = ["hermes", "-p", profile_arg, "--cli"]
    cmd.extend(["chat", "-q", f"work kanban task {task.id}"])
    if task.goal_mode:
        cmd.append("-Q")
    return cmd
'''

UPSTREAM = {
    CONNECT_RELATIVE: UPSTREAM_CONNECT,
    DB_RELATIVE: UPSTREAM_DB,
    SCHEMAS_RELATIVE: UPSTREAM_SCHEMAS,
    TOOLS_RELATIVE: UPSTREAM_TOOLS,
    DISPATCH_RELATIVE: UPSTREAM_DISPATCH,
}


class KanbanCardMaxTurnsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "hermes"
        for relative, text in UPSTREAM.items():
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def apply_quietly(self):
        with contextlib.redirect_stdout(io.StringIO()):
            apply(self.root)

    def text(self, relative):
        return (self.root / relative).read_text(encoding="utf-8")

    def argv(self, **task):
        namespace = {}
        exec(compile(self.text(DISPATCH_RELATIVE), DISPATCH_RELATIVE, "exec"), namespace)
        fields = {"id": "t1", "goal_mode": False, "max_turns": None, **task}
        return namespace["_worker_argv"](SimpleNamespace(**fields), "platform", None)

    def test_every_file_carries_the_marker(self):
        self.apply_quietly()
        for relative in UPSTREAM:
            with self.subTest(file=relative):
                self.assertIn(BUILD_MARKER, self.text(relative))

    def test_the_schema_and_migration_both_gain_the_column(self):
        self.apply_quietly()
        self.assertIn('("max_turns", "max_turns INTEGER")', self.text(CONNECT_RELATIVE))
        self.assertIn("    max_turns            INTEGER,", self.text(DB_RELATIVE))

    def test_create_task_and_the_insert_carry_it_with_matching_placeholders(self):
        self.apply_quietly()
        db = self.text(DB_RELATIVE)
        self.assertIn("    max_turns: Optional[int] = None,", db)
        columns = db[db.index("INSERT INTO tasks (") : db.index(") VALUES")]
        placeholders = db[db.index(") VALUES (") : db.index(")", db.index(") VALUES (") + len(") VALUES ("))]
        self.assertEqual(columns.count(",") + 1, placeholders.count("?"))
        self.assertEqual(placeholders.count("?"), 25)
        self.assertIn("_opt_int(max_turns),", db)

    def test_the_tool_exposes_and_passes_it(self):
        self.apply_quietly()
        self.assertIn('"max_turns": _prop("integer"', self.text(SCHEMAS_RELATIVE))
        self.assertIn('max_turns=_opt_int(args.get("max_turns"))', self.text(TOOLS_RELATIVE))

    def test_a_card_with_a_budget_spawns_its_worker_under_it_after_chat(self):
        self.apply_quietly()
        argv = self.argv(max_turns=250)
        self.assertEqual(argv[argv.index("--max-turns") + 1], "250")
        self.assertGreater(argv.index("--max-turns"), argv.index("chat"))

    def test_a_card_without_a_budget_spawns_as_before(self):
        self.apply_quietly()
        self.assertNotIn("--max-turns", self.argv())

    def test_a_non_positive_or_non_int_budget_is_ignored(self):
        self.apply_quietly()
        for bad in (0, -5, True, "250", 2.5):
            with self.subTest(value=bad):
                self.assertNotIn("--max-turns", self.argv(max_turns=bad))

    def test_a_second_run_is_refused(self):
        self.apply_quietly()
        with self.assertRaises(SystemExit) as caught:
            apply(self.root)
        self.assertIn(BUILD_MARKER, str(caught.exception))

    def test_a_moved_anchor_fails_the_build_before_any_file_is_written(self):
        target = self.root / DISPATCH_RELATIVE
        target.write_text(UPSTREAM_DISPATCH.replace('"-q", f"work', '"--query", f"work'), encoding="utf-8")
        with self.assertRaises(SystemExit):
            apply(self.root)
        for relative in UPSTREAM:
            with self.subTest(file=relative):
                self.assertNotIn(BUILD_MARKER, self.text(relative))


if __name__ == "__main__":
    unittest.main()
