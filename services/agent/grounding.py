#!/usr/bin/env python3
"""Install one agent's identity and briefing into ``$HERMES_HOME`` at boot.

One image serves several agents. ``AGENT_ID`` picks which; everything else about
the container is identical — the toolset, the supervisor, the MCP mounts.

Where each kind of grounding belongs, and why:

* ``SOUL.md``  — identity and voice. Loaded from ``HERMES_HOME`` every session.
* ``AGENTS.md`` — durable facts, composed here from ``grounding/shared`` (what
  every HLT agent needs) plus ``grounding/<agent>`` (what this one needs). Hermes
  reads it from ``terminal.cwd`` via project-context discovery.
* ``MEMORY.md`` — genuinely learned deltas only. Deliberately NOT seeded: it is
  ~2200 chars, frozen per session and agent-writable, so it is the wrong
  container for a company knowledge base. An earlier version of this file wrote
  ``$HERMES_HOME/memory/*.md``; Hermes reads ``$HERMES_HOME/memories/MEMORY.md``,
  so nothing ever read those.

Composing AGENTS.md rather than shipping one per agent means the estate facts are
written once. Two agents disagreeing about canon is a bug, not a feature.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Lets a later boot recognise a file it wrote and refresh it, while leaving a
# hand-edited one alone. Hermes extends the same courtesy to its own default.
MARKER = "<!-- managed-by: hlt-agent-boot -->"

# Markers this boot used to write. Renaming the marker without honouring the old
# one orphans every file already on the disk: install() reads it as a hand-edit,
# preserves it, and the container silently keeps serving the previous agent's
# identity. That is exactly what happened switching this box from Brian to Cleo.
LEGACY_MARKERS = ("<!-- managed-by: hlt-brian-boot -->",)


def _is_managed(text: str) -> bool:
    return MARKER in text or any(marker in text for marker in LEGACY_MARKERS)

GROUNDING_SRC = Path(__file__).resolve().parent / "grounding"
AGENT_IDS = ("cleo", "brian")
DEFAULT_AGENT = "cleo"


def resolve_agent(env: Any = None) -> str:
    """Which agent this container is.

    An unrecognised ``AGENT_ID`` falls back to the default rather than crashing,
    but the fact is reported in ``/health`` — a typo must not silently boot the
    wrong persona into a Slack workspace unnoticed.
    """
    env = os.environ if env is None else env
    requested = (env.get("AGENT_ID") or "").strip().lower()
    return requested if requested in AGENT_IDS else DEFAULT_AGENT


def grounding_dir(home: Path) -> Path:
    """Where the composed AGENTS.md lands; this is Hermes' ``terminal.cwd``."""
    return home / "grounding"


def install(
    agent: str | None = None,
    home: str | os.PathLike[str] | None = None,
    env: Any = None,
) -> dict[str, Any]:
    """Write SOUL.md and the composed AGENTS.md. Returns a summary for /health."""
    env = os.environ if env is None else env
    requested = (env.get("AGENT_ID") or "").strip().lower()
    agent = agent or resolve_agent(env)

    home_path = Path(home or env.get("HERMES_HOME") or "/data/hermes")
    home_path.mkdir(parents=True, exist_ok=True)
    target = grounding_dir(home_path)
    target.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "agent": agent,
        "agent_id_unrecognised": bool(requested) and requested not in AGENT_IDS,
        "soul_installed": False,
        "soul_preserved_operator_edit": False,
        "briefing_sections": [],
        "skills_installed": [],
    }

    # --- identity ---------------------------------------------------------
    soul_src = GROUNDING_SRC / agent / "SOUL.md"
    soul_dest = home_path / "SOUL.md"
    if soul_src.is_file():
        existing = ""
        if soul_dest.exists():
            try:
                existing = soul_dest.read_text(encoding="utf-8")
            except OSError:
                existing = ""
        if existing and not _is_managed(existing):
            summary["soul_preserved_operator_edit"] = True
        else:
            soul_dest.write_text(
                f"{MARKER}\n{soul_src.read_text(encoding='utf-8')}", encoding="utf-8"
            )
            summary["soul_installed"] = True

    # --- briefing ---------------------------------------------------------
    # Shared first: read the estate before your own role.
    #
    # There is deliberately no TEAM.md here. `agent_doc:global-team-context` in
    # the Katailyst registry already owns "who is on the team and how to adapt
    # per person" for the whole fleet, and the standing instructions are
    # explicit: check the registry before creating a new entity. A local copy
    # is a second canon that drifts.
    parts: list[str] = []
    for name, path in (
        ("shared", GROUNDING_SRC / "shared" / "AGENTS.md"),
        (agent, GROUNDING_SRC / agent / "AGENTS.md"),
    ):
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8").rstrip())
            summary["briefing_sections"].append(name)

    # --- skills -----------------------------------------------------------
    # Hermes scans a "Skills (mandatory)" index before every reply and loads a
    # matching one on demand. She shipped with the `skills` toolset granted and
    # not one skill installed, so the index was empty every turn. Skills also
    # keep the always-on briefing small: procedure belongs here, not in
    # AGENTS.md, which is capped by `context_file_max_chars`.
    skills_src = GROUNDING_SRC / agent / "skills"
    skills_dest = home_path / "skills"
    installed: list[str] = []
    if skills_src.is_dir():
        for skill_dir in sorted(skills_src.iterdir()):
            body = skill_dir / "SKILL.md"
            if not body.is_file():
                continue
            dest_dir = skills_dest / skill_dir.name
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / "SKILL.md"
            existing = dest.read_text(encoding="utf-8") if dest.exists() else ""
            if existing and not _is_managed(existing):
                continue  # a hand-edited skill is the operator's, not ours
            dest.write_text(
                f"{body.read_text(encoding='utf-8')}\n\n{MARKER}\n", encoding="utf-8"
            )
            installed.append(skill_dir.name)
    summary["skills_installed"] = installed

    if parts:
        (target / "AGENTS.md").write_text(
            f"{MARKER}\n" + "\n\n---\n\n".join(parts) + "\n", encoding="utf-8"
        )

    return summary


def main() -> None:
    for key, value in install().items():
        print(f"[agent] grounding {key}: {value}")


if __name__ == "__main__":
    main()
