#!/usr/bin/env python3
"""Install Brian's identity file into ``$HERMES_HOME`` at boot.

Replaces the old ``seed_memory.py``, which copied ``seed/*.md`` into
``$HERMES_HOME/memory/``. Hermes reads memory from ``$HERMES_HOME/memories/``
(plural) and only ever from the fixed filenames ``MEMORY.md`` and ``USER.md`` —
so the old seeding wrote files that nothing read.

Where each kind of grounding actually belongs:

* ``SOUL.md``  — identity and voice. Loaded from ``HERMES_HOME`` every session.
  Installed here.
* ``AGENTS.md`` — durable company facts. Loaded by Hermes' project-context
  discovery from ``terminal.cwd`` (see ``render_config.GROUNDING_DIR``). It stays
  in the image, read-only, so the agent cannot rewrite its own briefing.
* ``MEMORY.md`` — genuinely learned deltas only. Agent-written, approval-gated,
  capped at ~2200 chars. Deliberately NOT seeded: it is frozen per session and
  is the wrong container for a company knowledge base.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Lets a later boot recognise a file it wrote and refresh it, while leaving a
# hand-edited SOUL.md alone. Hermes has the same courtesy for its own default.
MARKER = "<!-- managed-by: hlt-brian-boot -->"

GROUNDING_DIR = Path(__file__).resolve().parent / "grounding"


def install(home: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Write SOUL.md into HERMES_HOME. Returns a summary for ``/health``."""
    home_path = Path(home or os.getenv("HERMES_HOME") or "/data/hermes")
    home_path.mkdir(parents=True, exist_ok=True)

    source = GROUNDING_DIR / "SOUL.md"
    dest = home_path / "SOUL.md"
    summary: dict[str, Any] = {
        "soul_installed": False,
        "soul_preserved_operator_edit": False,
        "agents_md_present": (GROUNDING_DIR / "AGENTS.md").is_file(),
    }

    if not source.is_file():
        return summary

    if dest.exists():
        try:
            existing = dest.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if MARKER not in existing:
            summary["soul_preserved_operator_edit"] = True
            return summary

    dest.write_text(f"{MARKER}\n{source.read_text(encoding='utf-8')}", encoding="utf-8")
    summary["soul_installed"] = True
    return summary


def main() -> None:
    for key, value in install().items():
        print(f"[brian] grounding {key}: {value}")


if __name__ == "__main__":
    main()
