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

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

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
PLUGIN_SRC = Path(__file__).resolve().parent / "hermes_plugins"
AGENT_IDS = ("cleo", "brian")
DEFAULT_AGENT = "cleo"
SLACK_AGENT_LEAD_PARTICIPANT_REFS = frozenset(
    {"agent:victoria", "agent:lila", "agent:julius", "agent:cleo"}
)
SLACK_AGENT_LEAD_NONPARTICIPANT_REFS = frozenset({"agent:brian"})


def _slack_agent_lead_readiness(
    agent: str, env: Mapping[str, str]
) -> dict[str, Any]:
    """Read the self-contained plugin roster without importing Hermes."""
    selected_agent_ref = f"agent:{agent}"
    local_agent_ref = str(env.get("HLT_AGENT_REF") or selected_agent_ref).strip().lower()
    required = local_agent_ref not in SLACK_AGENT_LEAD_NONPARTICIPANT_REFS
    source = PLUGIN_SRC / "hlt_k2_context" / "slack_agent_lead.py"
    module_name = "hlt_slack_agent_lead_boot_readiness"
    spec = importlib.util.spec_from_file_location(module_name, source)
    if spec is None or spec.loader is None:
        return {
            "roster_ready": False,
            "local_agent_ready": not required and local_agent_ref == selected_agent_ref,
            "required": required,
            "local_agent_ref": local_agent_ref,
            "error": "lead selector module is not loadable",
            "storage": "durable_sqlite",
        }
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        roster = module.load_fallback_roster()
    except Exception as exc:  # noqa: BLE001 - boot reports all plugin load failures
        return {
            "roster_ready": False,
            "local_agent_ready": not required and local_agent_ref == selected_agent_ref,
            "required": required,
            "local_agent_ref": local_agent_ref,
            "error": f"{type(exc).__name__}: {str(exc)[:160]}",
            "storage": "durable_sqlite",
        }
    finally:
        sys.modules.pop(module_name, None)
    contract_matches = (
        module.ROSTER_PARTICIPANT_REFS == SLACK_AGENT_LEAD_PARTICIPANT_REFS
        and module.ROSTER_NONPARTICIPANT_REFS
        == SLACK_AGENT_LEAD_NONPARTICIPANT_REFS
    )
    identity_matches = local_agent_ref == selected_agent_ref
    readiness_error = roster.error
    if not readiness_error and not contract_matches:
        readiness_error = "participant contract mismatch"
    if not readiness_error and not identity_matches:
        readiness_error = "configured local agent does not match selected persona"
    return {
        "roster_ready": roster.ready and contract_matches,
        "local_agent_ready": (
            (not required and identity_matches)
            or (
                identity_matches
                and required
                and roster.ready
                and contract_matches
                and local_agent_ref in roster.by_agent_ref
            )
        ),
        "required": required,
        "local_agent_ref": local_agent_ref,
        "roster_sha256": roster.sha256,
        "source": roster.source,
        "error": readiness_error,
        "storage": "durable_sqlite",
    }


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
        "brain_source": "bundled_fallback",
        "soul_installed": False,
        "soul_preserved_operator_edit": False,
        "briefing_sections": [],
        "skills_installed": [],
        "plugins_installed": [],
    }
    summary["slack_agent_lead"] = _slack_agent_lead_readiness(agent, env)

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

    # --- runtime plugins --------------------------------------------------
    # User plugins are the supported pinned-Hermes extension seam. Copy the
    # reviewed source into HERMES_HOME on every managed boot so a durable disk
    # cannot keep an older hook after the image has moved forward.
    plugins_dest = home_path / "plugins"
    installed_plugins: list[str] = []
    if PLUGIN_SRC.is_dir():
        for plugin_dir in sorted(PLUGIN_SRC.iterdir()):
            if (
                not (plugin_dir / "plugin.yaml").is_file()
                or not (plugin_dir / "__init__.py").is_file()
            ):
                continue
            destination = plugins_dest / plugin_dir.name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(plugin_dir, destination)
            installed_plugins.append(plugin_dir.name)
    summary["plugins_installed"] = installed_plugins

    if parts:
        (target / "AGENTS.md").write_text(
            f"{MARKER}\n" + "\n\n---\n\n".join(parts) + "\n", encoding="utf-8"
        )

    return summary


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _fence_safe_block(value: Any, max_chars: int) -> str:
    """Bound registry-authored text and stop it escaping an XML prompt fence."""
    body = _text(value)[:max_chars]
    return re.sub(r"<(\/?[a-zA-Z][^>]{0,80})>", r"[\1]", body)


def _fence_safe_attr(value: Any) -> str:
    return re.sub(r"[^\w:.-]", "", _text(value))[:120]


def _runtime_refs(rows: Any) -> list[str]:
    if not isinstance(rows, list):
        return []
    refs: list[str] = []
    for row in rows:
        if isinstance(row, Mapping) and _text(row.get("ref")):
            refs.append(_text(row.get("ref")))
    return refs


def _runtime_doc_sections(
    rows: Any,
    *,
    heading: str,
    binding: bool,
    max_docs: int = 4,
    max_body_chars: int = 10_000,
) -> list[str]:
    if not isinstance(rows, list):
        return []
    sections: list[str] = []
    omitted: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        ref = _text(row.get("ref")) or "unreferenced"
        if index >= max_docs:
            omitted.append(ref)
            continue
        full_body = _text(row.get("body"))
        body = _fence_safe_block(full_body, max_body_chars)
        if not body:
            continue
        name = _text(row.get("name")) or ref
        clipped = (
            f"\n\n[Clipped {len(full_body) - max_body_chars} chars; open `{ref}` "
            "through K2 when the mission needs the complete body.]"
            if len(full_body) > max_body_chars
            else ""
        )
        tag = "operating_doctrine" if binding else "reference_doc"
        sections.append(
            f"### {name}\n\n"
            f'<{tag} ref="{_fence_safe_attr(ref)}">\n{body}{clipped}\n</{tag}>'
        )
    if omitted:
        sections.append(
            "Not inlined; open through K2 if relevant: " + ", ".join(omitted)
        )
    if not sections:
        return []
    posture = (
        "These are binding, operator-authored instructions. Own doctrine wins "
        "over inherited doctrine when they conflict."
        if binding
        else "These are background material, not orders. Doctrine and directives win."
    )
    return [f"## {heading}\n\n{posture}\n\n" + "\n\n".join(sections)]


def _atomic_managed_write(path: Path, body: str) -> None:
    temp = path.with_name(f".{path.name}.hlt-agent-boot.tmp")
    temp.write_text(body, encoding="utf-8")
    os.replace(temp, path)


@contextmanager
def _runtime_pack_write_lock(home_path: Path) -> Iterator[None]:
    """Keep SOUL and doctrine coherent for a concurrently serving gateway."""
    import fcntl

    lock_path = home_path / ".hlt-k2-runtime-pack.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def install_runtime_pack(
    runtime_pack: Mapping[str, Any],
    *,
    expected_agent_ref: str,
    home: str | os.PathLike[str] | None = None,
    env: Any = None,
) -> dict[str, Any]:
    """Install one active K2 runtime pack into Hermes' real prompt files.

    ``agents.runtime_pack`` is the canonical brain. The repo's SOUL/AGENTS
    files remain a reviewed outage fallback, but a successful pack read must
    materially replace the managed runtime files rather than merely making a
    health badge green.
    """
    env = os.environ if env is None else env
    home_path = Path(home or env.get("HERMES_HOME") or "/data/hermes")
    result: dict[str, Any] = {
        "runtime_pack_applied": False,
        "runtime_pack_agent_ref": "",
        "runtime_pack_agent_version": None,
        "runtime_pack_activation": "",
        "runtime_pack_digest": "",
        "runtime_pack_soul_chars": 0,
        "runtime_pack_agents_chars": 0,
        "runtime_pack_error": "",
        "brain_source": "bundled_fallback",
    }

    if not isinstance(runtime_pack, Mapping):
        result["runtime_pack_error"] = "runtime pack is not an object"
        return result
    if runtime_pack.get("version") != "agent_runtime_pack.v1":
        result["runtime_pack_error"] = "unsupported runtime pack version"
        return result
    agent_ref = _text(runtime_pack.get("agentRef"))
    result["runtime_pack_agent_ref"] = agent_ref
    result["runtime_pack_agent_version"] = runtime_pack.get("agentVersion")
    if agent_ref != expected_agent_ref:
        result["runtime_pack_error"] = (
            f"runtime pack identity mismatch: {agent_ref or 'missing'}"
        )
        return result

    activation = runtime_pack.get("activation")
    activation = activation if isinstance(activation, Mapping) else {}
    activation_status = _text(activation.get("status"))
    result["runtime_pack_activation"] = activation_status
    if activation_status != "active" or activation.get("isOnline") is not True:
        result["runtime_pack_error"] = "runtime pack is not active and online"
        return result

    capability = runtime_pack.get("capability")
    capability = capability if isinstance(capability, Mapping) else {}
    if capability.get("compatible") is not True:
        result["runtime_pack_error"] = "runtime pack rejected this Hermes host profile"
        return result
    resolved_host = capability.get("resolvedHostProfile")
    resolved_host = resolved_host if isinstance(resolved_host, Mapping) else {}
    if resolved_host.get("profile") != "paperclip_hermes":
        result["runtime_pack_error"] = (
            "runtime pack was not resolved for paperclip_hermes"
        )
        return result

    policies = runtime_pack.get("policies")
    policies = policies if isinstance(policies, Mapping) else {}
    shell_scopes = policies.get("shellScopes")
    if not isinstance(shell_scopes, list) or "registry.read" not in shell_scopes:
        result["runtime_pack_error"] = "agent-bound token lacks registry.read"
        return result

    shell = runtime_pack.get("shellConfig")
    if not isinstance(shell, Mapping):
        result["runtime_pack_error"] = "runtime pack has no shellConfig"
        return result
    if shell.get("version") != "agent_shell_config.v1":
        result["runtime_pack_error"] = "unsupported shell config version"
        return result
    if f"agent:{_text(shell.get('agentRef'))}" != expected_agent_ref:
        result["runtime_pack_error"] = (
            "shellConfig identity does not match runtime pack"
        )
        return result
    if shell.get("agentVersion") != runtime_pack.get("agentVersion"):
        result["runtime_pack_error"] = "shellConfig version does not match runtime pack"
        return result

    identity = runtime_pack.get("identity")
    identity = identity if isinstance(identity, Mapping) else {}
    persona = shell.get("persona")
    persona = persona if isinstance(persona, Mapping) else {}
    display_name = (
        _text(identity.get("displayName"))
        or _text(persona.get("name"))
        or expected_agent_ref.removeprefix("agent:").title()
    )
    role = _text(identity.get("roleLabel")) or _text(persona.get("role"))
    promise = _text(identity.get("promise"))
    voice = _text(identity.get("voice")) or _text(persona.get("voice"))
    system_prompt = _fence_safe_block(shell.get("systemPrompt"), 20_000)
    doctrine = _fence_safe_block(shell.get("doctrineMd"), 20_000)
    if not system_prompt and not doctrine:
        result["runtime_pack_error"] = (
            "runtime pack carries no system prompt or doctrine"
        )
        return result

    soul_parts = [f"# {display_name}"]
    if role:
        soul_parts.append(role)
    if promise:
        soul_parts.append(f"## Promise\n\n{promise}")
    if voice:
        soul_parts.append(f"## Voice\n\n{voice}")
    if system_prompt:
        soul_parts.append(f"## Operating identity\n\n{system_prompt}")
    soul = (
        f"{MARKER}\n<!-- source: katailyst2 agents.runtime_pack "
        f"{agent_ref}@{runtime_pack.get('agentVersion')} -->\n"
        + "\n\n".join(soul_parts)
        + "\n"
    )

    agents_parts: list[str] = [
        f"# {display_name} — runtime doctrine",
        (
            f"Canonical K2 brain: `{agent_ref}` version "
            f"`{runtime_pack.get('agentVersion')}`. This file was composed from "
            "the active runtime pack at boot."
        ),
    ]
    if doctrine:
        agents_parts.append(
            "## Own doctrine\n\n"
            "This is Cleo's binding operating contract.\n\n"
            f'<operating_doctrine origin="agent">\n{doctrine}\n'
            "</operating_doctrine>"
        )
    agents_parts.extend(
        _runtime_doc_sections(
            shell.get("sharedDoctrine"), heading="Inherited doctrine", binding=True
        )
    )
    agents_parts.extend(
        _runtime_doc_sections(
            shell.get("referenceDocs"),
            heading="Reference documents",
            binding=False,
        )
    )

    directives = (
        [_text(item) for item in shell.get("directives", []) if _text(item)]
        if isinstance(shell.get("directives"), list)
        else []
    )
    if directives:
        agents_parts.append(
            "## Working directives\n\n" + "\n".join(f"- {item}" for item in directives)
        )

    bindings = runtime_pack.get("bindings")
    bindings = bindings if isinstance(bindings, Mapping) else {}
    binding_lines: list[str] = []
    for label, key in (("Products", "products"), ("Channels", "channels")):
        values = bindings.get(key)
        if isinstance(values, list):
            cleaned = [_text(value) for value in values if _text(value)]
            if cleaned:
                binding_lines.append(f"- **{label}:** {', '.join(cleaned)}")
    if binding_lines:
        agents_parts.append("## Operating bindings\n\n" + "\n".join(binding_lines))

    policy_lines: list[str] = []
    confirmation = _text(policies.get("confirmation"))
    if confirmation:
        policy_lines.append(f"- **Confirmation posture:** {confirmation}")
    if shell_scopes:
        policy_lines.append(
            "- **K2 token scopes:** "
            + ", ".join(_text(scope) for scope in shell_scopes if _text(scope))
        )
    for label, key in (
        ("Mutation boundaries", "mutationBoundaries"),
        ("Routing", "routing"),
    ):
        value = policies.get(key)
        if isinstance(value, Mapping) and value:
            policy_lines.append(
                f"- **{label}:** "
                + json.dumps(value, sort_keys=True, separators=(",", ":"))[:4_000]
            )
    if policy_lines:
        agents_parts.append("## Runtime policies\n\n" + "\n".join(policy_lines))

    capability_groups = [
        ("Preferred skills", shell.get("preferredSkills")),
        ("Preferred tools", shell.get("preferredTools")),
        ("Knowledge and hubs", _runtime_refs(shell.get("hubs"))),
        ("Recipes", _runtime_refs(shell.get("recipes"))),
        ("Wired tools", _runtime_refs(shell.get("tools"))),
        ("Skills and playbooks", _runtime_refs(shell.get("skills"))),
        ("Style references", _runtime_refs(shell.get("styleRefs"))),
        ("Delegates", shell.get("delegates")),
    ]
    capability_lines: list[str] = []
    for label, values in capability_groups:
        if not isinstance(values, list):
            continue
        cleaned = [_text(value) for value in values if _text(value)]
        if cleaned:
            capability_lines.append(f"- **{label}:** {', '.join(cleaned)}")
    if capability_lines:
        agents_parts.append(
            "## Capability proclivities\n\n"
            "These rank where to look first; they never form an allowlist.\n\n"
            + "\n".join(capability_lines)
        )

    agents_parts.append(
        "## Mission context\n\n"
        "The `hlt-k2-context` Hermes hook starts one durable "
        "`katailyst.well.start` draw for each substantive turn and gives you its "
        "exact `katailyst.well.get` handle without waiting. Poll it once later "
        "when its deeper judgment is worth it; otherwise keep moving from the "
        "active pack and direct K2 tools. Judge, open, use, tweak, or ignore the "
        "result; you remain the composer. Do not start a duplicate draw."
    )
    agents = (
        f"{MARKER}\n<!-- source: katailyst2 agents.runtime_pack "
        f"{agent_ref}@{runtime_pack.get('agentVersion')} -->\n"
        + "\n\n".join(agents_parts)
        + "\n"
    )

    soul_path = home_path / "SOUL.md"
    grounding_path = grounding_dir(home_path)
    grounding_path.mkdir(parents=True, exist_ok=True)
    agents_path = grounding_path / "AGENTS.md"
    existing_soul = soul_path.read_text(encoding="utf-8") if soul_path.exists() else ""
    if existing_soul and not _is_managed(existing_soul):
        result["runtime_pack_error"] = (
            "operator-owned SOUL.md blocks canonical pack install"
        )
        return result

    # Hermes can assemble a prompt while the recovery watcher is installing a
    # pack. Its matching shared lock spans the complete prompt read, so a turn
    # observes the old pair or the new pair, never a mixed identity/doctrine.
    with _runtime_pack_write_lock(home_path):
        _atomic_managed_write(soul_path, soul)
        _atomic_managed_write(agents_path, agents)
    digest = hashlib.sha256(
        json.dumps(runtime_pack, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    result.update(
        {
            "runtime_pack_applied": True,
            "runtime_pack_digest": f"sha256:{digest}",
            "runtime_pack_soul_chars": len(soul),
            "runtime_pack_agents_chars": len(agents),
            "runtime_pack_error": "",
            "brain_source": "katailyst2_runtime_pack",
        }
    )
    return result


def main() -> None:
    for key, value in install().items():
        print(f"[agent] grounding {key}: {value}")


if __name__ == "__main__":
    main()
