#!/usr/bin/env python3
"""Build-time behaviour gate for the per-card turn-budget patch.

Run by ``deploy/docker/Dockerfile`` against the patched ``/opt/hermes`` tree,
after ``apply_kanban_card_max_turns.py``. The applier proves its anchors
matched; this imports the shipped modules and asserts on behaviour:

- a fresh board stores and reads back a card's ``max_turns``;
- a board created before the column existed gains it on open;
- the worker command carries ``--max-turns N`` for such a card and no flag
  for a card without one or with a non-positive value;
- the ``kanban_create`` schema exposes the argument and the handler passes
  it to ``create_task``.
"""

from __future__ import annotations

import inspect
import sqlite3
import sys
import tempfile
from pathlib import Path

CARD_CAP = 12
TMP = Path(tempfile.mkdtemp())


def main(root: Path) -> None:
    sys.path.insert(0, str(root))
    import hermes_cli.kanban_db as KB
    import hermes_cli.kanban_db_connect as KC
    import hermes_cli.kanban_db_dispatch as KD
    import tools.kanban_tools as KT
    import tools.kanban_tools_schemas as KS

    problems = []

    db = TMP / "fresh.db"
    KC.init_db(db)
    conn = KC.connect(db)
    with_cap = KB.create_task(conn, title="cap", assignee="platform", max_turns=CARD_CAP)
    without = KB.create_task(conn, title="plain", assignee="platform")
    zero = KB.create_task(conn, title="zero", assignee="platform", max_turns=0)
    if KB.get_task(conn, with_cap).max_turns != CARD_CAP:
        problems.append("a card's max_turns did not read back")
    if KB.get_task(conn, without).max_turns is not None:
        problems.append("a card without max_turns did not read back None")

    legacy = TMP / "legacy.db"
    KC.init_db(legacy)
    raw = sqlite3.connect(legacy)
    raw.execute("ALTER TABLE tasks DROP COLUMN max_turns")
    raw.commit()
    raw.close()
    KC.init_db(legacy)
    if "max_turns" not in {r[1] for r in sqlite3.connect(legacy).execute("PRAGMA table_info(tasks)")}:
        problems.append("a board created before the column existed did not gain it on open")

    KD._resolve_hermes_argv = lambda: ["hermes"]
    KD._resolve_worker_cli_toolsets = lambda hermes_home: []
    argv_cap = KD._worker_argv(KB.get_task(conn, with_cap), "platform", None)
    argv_plain = KD._worker_argv(KB.get_task(conn, without), "platform", None)
    argv_zero = KD._worker_argv(KB.get_task(conn, zero), "platform", None)
    if "--max-turns" not in argv_cap or argv_cap[argv_cap.index("--max-turns") + 1] != str(CARD_CAP):
        problems.append(f"the worker command for a card with max_turns lacks --max-turns {CARD_CAP}: {argv_cap}")
    if argv_cap.index("--max-turns") < argv_cap.index("chat"):
        problems.append("--max-turns precedes the chat subcommand, where the parser does not take it")
    if "--max-turns" in argv_plain:
        problems.append("a card without max_turns got a --max-turns flag")
    if "--max-turns" in argv_zero:
        problems.append("a card with max_turns 0 got a --max-turns flag")

    if "max_turns" not in inspect.getsource(KS):
        problems.append("kanban_create schema does not expose max_turns")
    if 'max_turns=_opt_int(args.get("max_turns"))' not in inspect.getsource(KT):
        problems.append("the kanban_create handler does not pass max_turns to create_task")

    if problems:
        raise SystemExit("verify_kanban_card_max_turns:\n" + "\n".join(problems))
    print(
        "verify_kanban_card_max_turns: ok (column stored, read back and migrated; "
        "--max-turns after chat for a positive card value only; tool schema and handler carry it)"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
