"""Check a deployment's saved inputs against the inputs its script declares now.

A script on this server is a source plus the compiled program beside it. When a
new version of that program is saved, the inputs a deployment stored against the
old one are carried over untouched: the same keys with the same values. Some of
them are still exactly what the new script declares, some name something the
new script no longer declares, and some still parse but no longer mean what they
did - a quantity the script now reads in another unit, a period whose bounds
moved, a choice taken out of the list. A backtest runs either way; a live order
is sized by whatever the carried value says in the new script.

So a deployment's inputs are reconciled with the program it runs, in one place
and from the one document that states them, at the two moments that matter:

- **When the run settings are saved**, the submitted values are checked against
  the program on disk *before* anything is written, and each one is put into
  one of three lists: ``keep`` (still valid as it is), ``default`` (no longer
  declared, or newly declared and left to the script's own value) and ``reset``
  (declared, but the carried value fails the new kind, bounds or choices, or the
  declaration itself changed since the value was set, so it has to be looked at
  and entered again). Nothing is saved until the trader confirms; the write is
  the settings store's one atomic write, so a refusal leaves the old settings
  exactly as they were.
- **Before every start**, the stored inputs are reconciled again against the
  program that is on disk *at that moment*. This is deliberately server side and
  reads the same file the run is about to load: a start from the panel, a start
  from the schedule and a start put back after a restart all pass through it, and
  so does a trader opening the deployment from another machine. Nothing in the
  check lives in a browser.

The fingerprint each saved value was checked against is stored beside it. It
captures the declaration, kind, bounds, choices, step and default, not the
value: a value that still satisfies every bound the new program states but whose
declaration changed is the "the same number means another quantity" case, and
the fingerprint is the only thing on disk that can see it. Deployments written
before this check existed carry no fingerprints, so nothing is claimed about a
change there; what can still be checked - the keys, the kinds and the bounds the
new program states - is.

This module reads a program as data, the way the runner does. It never imports
the engine and never evaluates a program: the declaration list is plain JSON the
compiler wrote.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from services import openscript_store
from services.openscript_run_config import is_script_name
from utils.logging import get_logger

logger = get_logger(__name__)

#: The three buckets a reviewed value lands in, in the words the answer uses.
KEEP = "keep"
DEFAULT = "default"
RESET = "reset"

#: Why one value landed in ``default`` or ``reset``. Read by the panel and by
#: the sentence a start is refused with, so a trader is told what to do rather
#: than only that something is stale.
REMOVED = "removed"  # saved key is no longer declared
ADDED = "added"  # declared now, no saved value, a usable default exists
KIND = "kind"  # the value is not the kind the declaration states
BOUNDS = "bounds"  # outside the declared minimum or maximum
OPTIONS = "options"  # not one of the declared choices
EMPTY = "empty"  # a declared value that needs text was saved blank
MISSING = "missing"  # declared, no saved value, and no usable default
CHANGED = "changed"  # still valid, but the declaration changed since it was set

#: The kinds a compiler emits for an ``input()``. A kind this code is older than
#: is checked structurally rather than refused: an unknown kind may carry a
#: perfectly good value, and refusing the check over the name of a kind would
#: block every deployment whenever the language adds one.
NUMBER = "number"
BOOL = "bool"
STRING = "string"
TIME = "time"
KNOWN_KINDS = (NUMBER, BOOL, STRING, TIME)


# ---------------------------------------------------------------------------
# Reading the program
# ---------------------------------------------------------------------------


def program_path(script: str):
    """The compiled program stored beside one source."""
    return openscript_store.program_path(openscript_store.script_dir(), script)


def read_program(script: str) -> dict[str, Any] | None:
    """One script's compiled program, or nothing when it is absent or unreadable.

    Nothing raises. A missing program is a state the start gate already names;
    an unreadable one is reported by the runner when the run loads. This check
    refuses to invent declarations for either, which is the only honest answer.
    """
    if not is_script_name(script):
        return None
    try:
        raw = program_path(script).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("Could not read the compiled program for %s", script)
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.exception("The compiled program for %s does not read as JSON", script)
        return None
    return parsed if isinstance(parsed, dict) else None


def source_hash_of(program: dict[str, Any] | None) -> str:
    """The hash the program records for the source it was compiled from."""
    source = program.get("source") if program else None
    recorded = source.get("hash") if isinstance(source, dict) else None
    return recorded if isinstance(recorded, str) else ""


def declarations_of(program: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every input declaration a program carries, as plain records.

    Read defensively, the same way the browser reads them: a program is JSON off
    a compiler and a shape nobody expected must not take the settings page or a
    start down. A record that cannot name a key is dropped, because the key is
    what a saved value is reconciled against.
    """
    raw = (program or {}).get("inputs")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for one in raw:
        if not isinstance(one, dict):
            continue
        key = str(one.get("key") or "")
        if not key:
            continue
        out.append(
            {
                "key": key,
                "kind": str(one.get("kind") or ""),
                "label": str(one.get("label") or key),
                "default": one.get("default"),
                "min": one.get("min") if isinstance(one.get("min"), (int, float)) else None,
                "max": one.get("max") if isinstance(one.get("max"), (int, float)) else None,
                "step": one.get("step") if isinstance(one.get("step"), (int, float)) else None,
                "options": list(one["options"]) if isinstance(one.get("options"), list) else None,
                "group": str(one.get("group") or ""),
                "tooltip": one.get("tooltip"),
            }
        )
    return out


def plain_default(declaration: dict[str, Any]) -> Any:
    """A declaration's default as the plain value a settings map holds.

    The program writes a constant as a tagged pair, ``["n", 10]`` for a number,
    ``["b", true]`` for a flag and ``["s", "x"]`` for text. An engine resolves a
    supplied setting against the declaration and wants the value itself, not the
    tag, so this returns the value itself - or nothing when the declaration
    states no usable default.
    """
    raw = declaration.get("default")
    if isinstance(raw, list) and len(raw) >= 2:
        value = raw[1]
        return value if isinstance(value, (bool, int, float, str)) else None
    if isinstance(raw, dict) and "value" in raw:
        value = raw.get("value")
        return value if isinstance(value, (bool, int, float, str)) else None
    return raw if isinstance(raw, (bool, int, float, str)) else None


def fingerprint_of(declaration: dict[str, Any]) -> str:
    """What the value was checked against, so a changed declaration is visible.

    Captures the declaration rather than the value: kind, bounds, step, choices
    and default. A value still inside new bounds whose declaration changed is
    the case this exists to name - the same number, read as another quantity or
    period. Canonical JSON keeps two records that say the same thing equal.
    """
    material = (
        declaration.get("kind"),
        declaration.get("min"),
        declaration.get("max"),
        declaration.get("step"),
        tuple(declaration["options"]) if declaration.get("options") is not None else None,
        plain_default(declaration),
    )
    text = json.dumps(material, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# One value against one declaration
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    # bool is a number in Python and nowhere in a script's type system.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def value_fault(declaration: dict[str, Any], value: Any) -> str | None:
    """Why a saved value cannot be carried into this declaration, or nothing.

    The declaration's own rules and not this file's opinion: the engine holds a
    supplied value to the same kind, minimum, maximum and choices before the
    first bar. Asking them here moves that refusal from a log written a minute
    later into the settings the trader is looking at. A kind this build does not
    know is accepted structurally, so a newer compiler cannot block a save.
    """
    kind = declaration.get("kind")

    if kind == BOOL:
        return None if isinstance(value, bool) else KIND
    if kind == NUMBER:
        if not _is_number(value):
            return KIND
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return KIND
        minimum = declaration.get("min")
        maximum = declaration.get("max")
        if _is_number(minimum) and number < float(minimum):
            return BOUNDS
        if _is_number(maximum) and number > float(maximum):
            return BOUNDS
        options = declaration.get("options")
        if options is not None and not _among_options(value, options):
            return OPTIONS
        return None
    if kind in (STRING, TIME):
        if not isinstance(value, str):
            return KIND
        if not value.strip():
            return EMPTY
        options = declaration.get("options")
        if options is not None and not _among_options(value, options):
            return OPTIONS
        return None

    # An unknown kind: accept the shapes any input could take. A list or a
    # nested object cannot be a setting any declaration resolves.
    if isinstance(value, bool) or _is_number(value) or isinstance(value, str):
        return None
    return KIND


def _among_options(value: Any, options: list[Any]) -> bool:
    """Whether a value is one of a declaration's choices.

    Direct equality first. A choice a compiler wrote as text and a value held as
    a number (or the reverse) are compared as text as well, because a form field
    holds text on every kind and a choice missed over that alone is a value the
    trader did pick.
    """
    if value in options:
        return True
    rendered = str(value)
    return any(isinstance(one, (bool, int, float, str)) and str(one) == rendered for one in options)


# ---------------------------------------------------------------------------
# The reconciliation
# ---------------------------------------------------------------------------


def _item(declaration: dict[str, Any], value: Any, reason: str) -> dict[str, Any]:
    """One reviewed input, as the answer carries it."""
    return {
        "key": declaration.get("key"),
        "label": declaration.get("label"),
        "kind": declaration.get("kind"),
        "saved": value,
        "default": plain_default(declaration),
        "reason": reason,
    }


def reconcile(
    declarations: list[dict[str, Any]],
    saved_inputs: dict[str, Any] | None,
    baseline: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Classify every saved value and every declaration against each other.

    ``baseline`` is the fingerprint map the saved values were last reconciled
    against. It is what lets a value that is still valid be named as a changed
    one; with no baseline nothing is claimed about a change.

    Returns the review and, in ``clean``, the values a confirmed save keeps:
    every value that is valid against a declaration as it stands now. Nothing is
    written by this function.
    """
    by_key = {one["key"]: one for one in declarations}
    saved = saved_inputs or {}
    baseline = baseline or {}

    keep: list[dict[str, Any]] = []
    defaults: list[dict[str, Any]] = []
    resets: list[dict[str, Any]] = []
    clean: dict[str, Any] = {}
    fingerprints: dict[str, str] = {}

    for key, value in saved.items():
        declaration = by_key.get(key)
        if declaration is None:
            # The new script no longer declares it. The carried value cannot be
            # passed on and is not a thing to re-type: it falls away, and the
            # script's new text decides instead.
            defaults.append(
                {
                    "key": key,
                    "label": key,
                    "kind": "",
                    "saved": value,
                    "default": None,
                    "reason": REMOVED,
                }
            )
            continue

        fault = value_fault(declaration, value)
        if fault is not None:
            # Still declared, but the carried value fails what it now states.
            # Silently substituting the script's default for a quantity or a
            # period the trader chose is the other way to get a position nobody
            # meant, so this one is re-entered rather than defaulted.
            resets.append(_item(declaration, value, fault))
            continue

        fingerprints[key] = fingerprint_of(declaration)
        clean[key] = value
        if key in baseline and baseline[key] != fingerprints[key]:
            resets.append(_item(declaration, value, CHANGED))
        else:
            keep.append(
                {
                    "key": key,
                    "label": declaration.get("label"),
                    "kind": declaration.get("kind"),
                    "value": value,
                }
            )

    # Declarations the saved settings never named. One with a usable default is
    # simply new; one without cannot run on anything and has to be filled.
    for declaration in declarations:
        key = declaration["key"]
        if key in saved:
            continue
        fingerprints[key] = fingerprint_of(declaration)
        if plain_default(declaration) is None:
            resets.append(_item(declaration, None, MISSING))
        elif baseline:
            # Only worth saying when there is a previous version to be newer
            # than. A deployment written before fingerprints existed would
            # otherwise list every input as "new" on its first review.
            defaults.append(_item(declaration, None, ADDED))

    return {
        "keep": keep,
        "default": defaults,
        "reset": resets,
        "clean": clean,
        "fingerprints": fingerprints,
    }


def review_for(
    script: str,
    saved_inputs: dict[str, Any] | None,
    baseline: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """The review of one set of inputs against the program on disk for a script.

    Answers nothing when the program cannot be read: the caller names that
    state itself, because "no declarations could be checked" and "everything
    checks out" must never be the same answer.
    """
    program = read_program(script)
    if program is None:
        return None
    declarations = declarations_of(program)
    result = reconcile(declarations, saved_inputs, baseline)
    result["script"] = script
    result["source_hash"] = source_hash_of(program)
    result["changed"] = bool(result["default"] or result["reset"])
    return result


def refusal_message(script: str, review: dict[str, Any]) -> str:
    """The sentence a start is refused with, naming what has to be reviewed.

    Written for the trader and not the log: it names the inputs and the one
    action that clears the refusal, which is opening the deployment's own
    settings and saving them once.
    """
    resets = [str(one.get("label") or one.get("key")) for one in review.get("reset", [])]
    defaults = [str(one.get("label") or one.get("key")) for one in review.get("default", [])]

    parts = [f"{script} changed since these run settings were saved."]
    if resets:
        parts.append(f"Re-enter {_listed(resets)}.")
    if defaults:
        parts.append(f"{_listed(defaults)} will follow the script's own value.")
    parts.append(
        "Open the deployment's Settings, review its inputs, and save them once before it starts."
    )
    return " ".join(parts)


def confirm_message(review: dict[str, Any]) -> str:
    """The sentence the save check is refused with, asking for one confirmation.

    Counts each bucket rather than naming every value, because the review beside
    the sentence already names them one by one; the sentence only has to say
    what the two saves are.
    """
    kept = len(review.get("keep", []))
    defaults = review.get("default", [])
    resets = review.get("reset", [])

    parts = []
    if kept:
        parts.append(f"{kept} can be kept")
    if defaults:
        parts.append(f"{len(defaults)} will follow the script's own value")
    if resets:
        parts.append(f"{len(resets)} must be re-entered")
    joined = "; ".join(parts) or "Nothing carries over unchanged"
    return (
        f"The script declares different inputs now: {joined}. Review the list, set anything "
        "marked for re-entry, then save again to confirm. Nothing was saved yet."
    )


def _listed(names: list[str]) -> str:
    """A list of input names, as a sentence says them."""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def start_refusal(saved: dict[str, Any]) -> str | None:
    """What blocks a start on stale inputs, or nothing when it may proceed.

    This is the gate every start passes through, whatever started it. A stored
    hash equal to the program's own is the unchanged fast path; anything else is
    reconciled and the first difference blocks the start. The settings are never
    rewritten here - a start is not a save, and an automatic rewrite on the way
    to the market would be a position sized without anybody confirming it.

    A program that is missing or unreadable is deliberately left to the checks
    that already own that state: the start route refuses one before the service
    is called, and a start put back after a restart hands it to the child, which
    fails in its own log by design. This gate refuses only on positive evidence,
    a program it could read and inputs that disagree with it.
    """
    script = str(saved.get("script") or "")
    if not is_script_name(script):
        return None

    program = read_program(script)
    if program is None:
        return None

    current_hash = source_hash_of(program)
    stored_hash = str(saved.get("source_hash") or "")
    if current_hash and stored_hash == current_hash:
        return None

    review = reconcile(
        declarations_of(program),
        saved.get("inputs") or {},
        saved.get("input_fingerprints") or {},
    )
    if not review["default"] and not review["reset"]:
        return None
    return refusal_message(script, review)
