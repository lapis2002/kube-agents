#!/usr/bin/env python3
"""Let a kanban card carry its own turn budget.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree; five files.

The gap (gke-labs/kube-agents#1947)
-----------------------------------
A kanban worker is spawned as ``hermes -p <profile> --cli chat -q ...`` and
reads its tool-calling iteration budget from the profile's
``agent.max_turns`` (150 on the Platform Agent). A cron job's roster entry
can carry its own ``max_turns`` (``apply_cron_job_max_turns.py``), but a
card cannot, so an audit stream asked for in chat runs under the worker
budget while the same stream on its schedule runs under its entry's 250.

The fix
-------
``hermes chat`` already takes ``--max-turns``, which outranks the profile's
config. The card gets a ``max_turns`` column (schema and the late-column
migration), ``create_task`` and the ``kanban_create`` tool accept it, and
the dispatcher appends ``--max-turns N`` to the worker command when the card
carries a positive integer. A card without one behaves exactly as before.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

# Text only a patched file contains.
BUILD_MARKER = "kube-agents patch: per-card turn budget"

CONNECT_RELATIVE = "hermes_cli/kanban_db_connect.py"
DB_RELATIVE = "hermes_cli/kanban_db.py"
SCHEMAS_RELATIVE = "tools/kanban_tools_schemas.py"
TOOLS_RELATIVE = "tools/kanban_tools.py"
DISPATCH_RELATIVE = "hermes_cli/kanban_db_dispatch.py"

# --- kanban_db_connect.py: the late-column migration list ------------------
MIGRATION = '    ("goal_max_turns", "goal_max_turns INTEGER"),\n'
MIGRATION_PATCHED = MIGRATION + (
    f"    # {BUILD_MARKER} (gke-labs/kube-agents#1947).\n"
    '    ("max_turns", "max_turns INTEGER"),\n'
)

# --- kanban_db.py: dataclass field, row read, schema, create_task, INSERT ---
FIELD = "    goal_max_turns: Optional[int] = None\n"
FIELD_PATCHED = FIELD + (
    f"    max_turns: Optional[int] = None          # {BUILD_MARKER}; NULL = profile's\n"
)

ROW_READ = (
    '    "model_override", "provider_override", "reasoning_effort", "goal_max_turns", "block_kind",\n'
    ")\n"
)
ROW_READ_PATCHED = (
    '    "model_override", "provider_override", "reasoning_effort", "goal_max_turns", "block_kind",\n'
    f'    "max_turns",  # {BUILD_MARKER}\n'
    ")\n"
)

SCHEMA = "    goal_max_turns       INTEGER,\n"
SCHEMA_PATCHED = SCHEMA + (
    f"    -- {BUILD_MARKER}: the worker's --max-turns. NULL = profile's.\n"
    "    max_turns            INTEGER,\n"
)

SIGNATURE = (
    '    goal_mode: bool = False, goal_max_turns: Optional[int] = None, initial_status: str = "running",\n'
)
SIGNATURE_PATCHED = SIGNATURE + (
    f"    max_turns: Optional[int] = None,  # {BUILD_MARKER}\n"
)

INSERT_COLUMNS = (
    "                        goal_mode, goal_max_turns, session_id, completion_contract\n"
    "                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n"
)
INSERT_COLUMNS_PATCHED = (
    "                        goal_mode, goal_max_turns, session_id, completion_contract,\n"
    "                        max_turns\n"
    "                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n"
)

INSERT_VALUES = (
    "                        1 if goal_mode else 0, _opt_int(goal_max_turns), session_id, completion_contract,\n"
    "                    ),\n"
)
INSERT_VALUES_PATCHED = (
    "                        1 if goal_mode else 0, _opt_int(goal_max_turns), session_id, completion_contract,\n"
    "                        _opt_int(max_turns),\n"
    "                    ),\n"
)

# --- kanban_tools_schemas.py: the kanban_create argument --------------------
PROP = '        "goal_max_turns": _prop("integer", (\n'
PROP_PATCHED = (
    f"        # {BUILD_MARKER} (gke-labs/kube-agents#1947).\n"
    '        "max_turns": _prop("integer", (\n'
    '                "The worker\'s own tool-calling iteration budget for this card "\n'
    '                "(hermes chat --max-turns); it replaces the assignee profile\'s "\n'
    '                "agent.max_turns for this card only. Omit it unless the work is "\n'
    '                "known to need more than the profile\'s budget, such as an audit "\n'
    '                "stream whose roster entry carries its own max_turns."\n'
    "        )),\n"
) + PROP

# --- kanban_tools.py: the create handler passes it through -------------------
HANDLER = '            goal_mode=goal_mode, goal_max_turns=_opt_int(args.get("goal_max_turns")),\n'
HANDLER_PATCHED = HANDLER + (
    f'            max_turns=_opt_int(args.get("max_turns")),  # {BUILD_MARKER}\n'
)

# --- kanban_db_dispatch.py: the worker command --------------------------------
ARGV = '    cmd.extend(["chat", "-q", f"work kanban task {task.id}"])\n'
ARGV_PATCHED = ARGV + (
    f"    # {BUILD_MARKER} (gke-labs/kube-agents#1947): a card's own\n"
    "    # max_turns is the worker's --max-turns, which outranks the profile's\n"
    "    # agent.max_turns. Anything but a positive integer is ignored.\n"
    "    _card_cap = task.max_turns\n"
    "    if isinstance(_card_cap, int) and not isinstance(_card_cap, bool) and _card_cap > 0:\n"
    '        cmd.extend(["--max-turns", str(_card_cap)])\n'
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason.

    Nothing is written until every anchor in all five files has matched.
    """
    patches = {
        relative: patchlib.Patch(root, relative, prefix="kanban_card_max_turns")
        for relative in (
            CONNECT_RELATIVE, DB_RELATIVE, SCHEMAS_RELATIVE, TOOLS_RELATIVE, DISPATCH_RELATIVE,
        )
    }
    for patch in patches.values():
        patch.refuse_if_patched(BUILD_MARKER)

    patches[CONNECT_RELATIVE].substitute(MIGRATION, MIGRATION_PATCHED, label="late-column migration")
    db = patches[DB_RELATIVE]
    db.substitute(FIELD, FIELD_PATCHED, label="Task field")
    db.substitute(ROW_READ, ROW_READ_PATCHED, label="row read columns")
    db.substitute(SCHEMA, SCHEMA_PATCHED, label="CREATE TABLE column")
    db.substitute(SIGNATURE, SIGNATURE_PATCHED, label="create_task signature")
    db.substitute(INSERT_COLUMNS, INSERT_COLUMNS_PATCHED, label="INSERT columns")
    db.substitute(INSERT_VALUES, INSERT_VALUES_PATCHED, label="INSERT values")
    patches[SCHEMAS_RELATIVE].substitute(PROP, PROP_PATCHED, label="kanban_create property")
    patches[TOOLS_RELATIVE].substitute(HANDLER, HANDLER_PATCHED, label="create handler")
    patches[DISPATCH_RELATIVE].substitute(ARGV, ARGV_PATCHED, label="worker argv")

    patches[CONNECT_RELATIVE].commit("1 anchor")
    db.commit("6 anchors")
    patches[SCHEMAS_RELATIVE].commit("1 anchor")
    patches[TOOLS_RELATIVE].commit("1 anchor")
    patches[DISPATCH_RELATIVE].commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
