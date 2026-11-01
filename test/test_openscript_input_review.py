"""Reviewing a deployment's saved inputs against the inputs its script declares now.

Every test below names the failure it catches. The one that matters most is the
headline one: a script saved in a newer version keeps the same input key with the
same numeric value, but the declaration around it changed, so the value means
another quantity or period in live trading while every check that only asks
"does it parse" still passes. The fingerprint is what sees that change.
"""

import json

import pytest
from flask import Flask

import blueprints.openscript_runner as runner
import services.openscript_inputs as review
import services.openscript_run_config as settings_store
import services.openscript_runner_service as service
import utils.session
from blueprints.openscript import openscript_bp
from blueprints.openscript_runner import openscript_runner_bp
from services import openscript_store

SCRIPT = "turn.oscript"
SYMBOL = "TESTSYM"
EXCHANGE = "EXCH"
INTERVAL = "5m"


def number_input(key, default=14, minimum=None, maximum=None, label=None, options=None):
    return {
        "key": key,
        "kind": "number",
        "label": label or key,
        "default": ["n", default],
        "min": minimum,
        "max": maximum,
        "step": 1,
        "options": options,
        "group": "",
        "tooltip": None,
    }


def string_input(key, default, options=None):
    return {
        "key": key,
        "kind": "string",
        "label": key,
        "default": ["s", default],
        "min": None,
        "max": None,
        "step": None,
        "options": options,
        "group": "",
        "tooltip": None,
    }


def bool_input(key, default=False):
    return {
        "key": key,
        "kind": "bool",
        "label": key,
        "default": ["b", default],
        "min": None,
        "max": None,
        "step": None,
        "options": None,
        "group": "",
        "tooltip": None,
    }


def program(source_hash, *inputs):
    return {
        "source": {"file": SCRIPT, "hash": source_hash, "lines": 1},
        "inputs": list(inputs),
        "meta": {"strategy": {}},
    }


def fingerprints_of(program_dict):
    return {
        one["key"]: review.fingerprint_of(one)
        for one in review.declarations_of(program_dict)
    }


# ---------------------------------------------------------------------------
# Reading declarations
# ---------------------------------------------------------------------------


def test_declarations_are_read_with_the_shape_a_value_is_checked_against():
    found = review.declarations_of(program("sha256:h", number_input("length", 14, 1, 200)))
    assert len(found) == 1
    declared = found[0]
    assert declared["key"] == "length"
    assert declared["kind"] == "number"
    assert declared["min"] == 1 and declared["max"] == 200
    assert review.plain_default(declared) == 14


def test_declarations_are_read_defensively():
    # A panel and a start both read this; one odd record from a newer compiler
    # must take neither down.
    found = review.declarations_of({"inputs": [None, "text", {}, {"kind": "number"}]})
    assert found == []
    assert review.declarations_of(None) == []
    assert review.declarations_of({"inputs": "nope"}) == []


def test_tagged_defaults_are_unwrapped_and_missing_ones_are_nothing():
    declared = {
        "key": "when",
        "kind": "time",
        "label": "When",
        "default": ["z", None],
        "min": None,
        "max": None,
        "step": None,
        "options": None,
    }
    assert review.plain_default(declared) is None
    assert review.plain_default({"default": ["s", "x"]}) == "x"
    assert review.plain_default({"default": ["b", True]}) is True
    assert review.plain_default({"default": 5}) == 5


# ---------------------------------------------------------------------------
# One value against one declaration
# ---------------------------------------------------------------------------


def test_a_value_is_held_to_the_declarations_own_kind_bounds_and_choices():
    length = number_input("length", 14, 1, 100)
    mode = string_input("mode", "fast", ["fast", "slow"])
    on = bool_input("on", True)

    assert review.value_fault(length, 20) is None
    assert review.value_fault(length, 0) == "bounds"
    assert review.value_fault(length, 101) == "bounds"
    assert review.value_fault(length, "20") == "kind"
    # A flag is never a number, even though Python says bool is one.
    assert review.value_fault(length, True) == "kind"
    assert review.value_fault(mode, "fast") is None
    assert review.value_fault(mode, "turbo") == "options"
    assert review.value_fault(on, True) is None
    assert review.value_fault(on, "true") == "kind"


def test_a_choice_written_as_text_matches_a_numeric_choice():
    # A form field holds text on every kind; the number kind still wants a
    # number, while a numeric choice matches a numeric value.
    declared = number_input("size", 1, options=[1, 2, 4])
    assert review.value_fault(declared, "2") == "kind"
    assert review.value_fault(declared, 2) is None
    assert review.value_fault(declared, 3) == "options"


def test_an_unknown_kind_is_checked_structurally():
    declared = {"key": "x", "kind": "future", "default": ["n", 1], "min": None,
                "max": None, "step": None, "options": None}
    assert review.value_fault(declared, 1) is None
    assert review.value_fault(declared, "text") is None
    assert review.value_fault(declared, [1]) == "kind"


# ---------------------------------------------------------------------------
# The reconciliation itself
# ---------------------------------------------------------------------------


def test_every_valid_value_is_kept_when_the_declaration_is_unchanged():
    old = program("sha256:v1", number_input("length", 14, 1, 100))
    result = review.reconcile(
        review.declarations_of(old), {"length": 20}, fingerprints_of(old)
    )
    assert [one["key"] for one in result["keep"]] == ["length"]
    assert result["default"] == [] and result["reset"] == []
    assert result["clean"] == {"length": 20}


def test_a_removed_input_falls_back_and_is_not_something_to_retype():
    old = program("sha256:v1", number_input("length"))
    new = program("sha256:v2")
    result = review.reconcile(
        review.declarations_of(new), {"length": 20, "gone": 1}, fingerprints_of(old)
    )
    assert result["keep"] == []
    assert [one["reason"] for one in result["default"]] == ["removed", "removed"]
    assert result["reset"] == []
    assert result["clean"] == {}


def test_a_value_the_new_declaration_rejects_must_be_re_entered():
    new = program("sha256:v2", number_input("length", 14, 1, 50), string_input("mode", "fast", ["fast"]))
    result = review.reconcile(
        review.declarations_of(new), {"length": 100, "mode": "turbo"}, {}
    )
    reasons = {one["key"]: one["reason"] for one in result["reset"]}
    assert reasons == {"length": "bounds", "mode": "options"}
    assert result["clean"] == {}


def test_a_value_that_is_still_valid_but_whose_declaration_changed_must_be_confirmed():
    # THE HEADLINE CASE. Same key, same 20, still inside the new bounds: nothing
    # a type check can catch. But the declaration moved, so 20 can be another
    # quantity or period in the new script.
    old = program("sha256:v1", number_input("length", 14, 1, 100))
    new = program("sha256:v2", number_input("length", 14, 1, 500))
    result = review.reconcile(
        review.declarations_of(new), {"length": 20}, fingerprints_of(old)
    )
    assert len(result["reset"]) == 1
    assert result["reset"][0]["reason"] == "changed"
    assert result["clean"] == {"length": 20}


def test_no_baseline_means_no_claim_about_a_change():
    # A deployment written before fingerprints existed must not be told every
    # still-valid value changed; the new kind and bounds are still checked.
    new = program("sha256:v2", number_input("length", 14, 1, 500))
    result = review.reconcile(review.declarations_of(new), {"length": 20}, {})
    assert [one["key"] for one in result["keep"]] == ["length"]
    assert result["reset"] == []


def test_newly_declared_inputs_use_their_default_but_are_named():
    old = program("sha256:v1", number_input("length"))
    new = program("sha256:v2", number_input("length"), number_input("factor", 3, 0, 10))
    result = review.reconcile(
        review.declarations_of(new), {"length": 20}, fingerprints_of(old)
    )
    added = [one for one in result["default"] if one["reason"] == "added"]
    assert [one["key"] for one in added] == ["factor"]


def test_a_declared_input_with_no_default_and_no_value_must_be_filled():
    declared = {
        "key": "qty",
        "kind": "number",
        "label": "qty",
        "default": ["z", None],
        "min": None,
        "max": None,
        "step": None,
        "options": None,
    }
    result = review.reconcile([declared], {}, {})
    assert result["reset"][0]["reason"] == "missing"


# ---------------------------------------------------------------------------
# The store carries the reconciliation evidence
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    scripts = tmp_path / "strategies" / "openscript"
    scripts.mkdir(parents=True)
    monkeypatch.setattr(openscript_store, "SCRIPTS_DIR", scripts)
    monkeypatch.setattr(settings_store, "CONFIG_FILE", tmp_path / "configs.json")
    return scripts


def save_deployment(program_dict, inputs=None, **rest):
    path = openscript_store.program_path(openscript_store.script_dir(), SCRIPT)
    path.write_text(json.dumps(program_dict), encoding="utf-8")
    fields = {
        "source_hash": program_dict["source"]["hash"],
        "input_fingerprints": fingerprints_of(program_dict),
    }
    fields.update(rest)
    return settings_store.write_run_config(
        SCRIPT, SYMBOL, EXCHANGE, INTERVAL, inputs=inputs, **fields
    )


def test_the_hash_and_fingerprints_are_saved_and_read_back():
    old = program("sha256:v1", number_input("length"))
    assert save_deployment(old, {"length": 20})[0]
    held = settings_store.read_run_config(SCRIPT)
    assert held["source_hash"] == "sha256:v1"
    assert held["input_fingerprints"] == fingerprints_of(old)


def test_an_older_settings_file_reads_with_empty_evidence():
    # An upgrade must not make every saved deployment unstartable; the gate just
    # claims nothing about a change it cannot evidence.
    settings_store.CONFIG_FILE.write_text(
        json.dumps(
            {
                SCRIPT: {
                    "script": SCRIPT,
                    "symbol": SYMBOL,
                    "exchange": EXCHANGE,
                    "interval": INTERVAL,
                    "inputs": {"length": 20},
                }
            }
        ),
        encoding="utf-8",
    )
    held = settings_store.read_run_config(SCRIPT)
    assert held["source_hash"] == ""
    assert held["input_fingerprints"] == {}
    assert held["inputs"] == {"length": 20}


def test_fingerprints_edited_into_the_file_by_hand_are_dropped():
    settings_store.CONFIG_FILE.write_text(
        json.dumps(
            {
                SCRIPT: {
                    "script": SCRIPT,
                    "symbol": SYMBOL,
                    "exchange": EXCHANGE,
                    "interval": INTERVAL,
                    "inputs": {},
                    "source_hash": 12345,
                    "input_fingerprints": {"length": ["not", "a", "fingerprint"]},
                }
            }
        ),
        encoding="utf-8",
    )
    held = settings_store.read_run_config(SCRIPT)
    assert held["source_hash"] == "12345"
    assert held["input_fingerprints"] == {}


# ---------------------------------------------------------------------------
# The start gate
# ---------------------------------------------------------------------------


def test_a_start_is_refused_while_saved_inputs_disagree_with_the_new_program(monkeypatch):
    old = program(
        "sha256:v1",
        number_input("length", 14, 1, 100),
        string_input("mode", "fast", ["fast", "slow"]),
    )
    assert save_deployment(old, {"length": 20, "mode": "fast"})[0]

    # The script is replaced on the server: one bound moved and one choice went.
    new = program(
        "sha256:v2",
        number_input("length", 14, 1, 500),
        string_input("mode", "fast", ["fast"]),
    )
    openscript_store.program_path(openscript_store.script_dir(), SCRIPT).write_text(
        json.dumps(new), encoding="utf-8"
    )

    spawned = []
    monkeypatch.setattr(
        service,
        "_spawn_claimed",
        lambda *a, **k: (spawned.append(a) or True, "started"),
    )

    ok, why = service.start_run(SCRIPT)
    assert not ok
    assert "length" in why and "Settings" in why
    assert spawned == []


def test_a_start_proceeds_once_the_settings_were_reconciled_and_saved(monkeypatch):
    old = program("sha256:v1", number_input("length", 14, 1, 100))
    assert save_deployment(old, {"length": 20})[0]
    new = program("sha256:v2", number_input("length", 14, 1, 500))
    openscript_store.program_path(openscript_store.script_dir(), SCRIPT).write_text(
        json.dumps(new), encoding="utf-8"
    )

    # Refused first ...
    monkeypatch.setattr(service, "_spawn_claimed", lambda *a, **k: (True, "started"))
    assert service.start_run(SCRIPT)[0] is False

    # ... and after the one confirmed save, started with the kept value.
    assert settings_store.write_run_config(
        SCRIPT,
        SYMBOL,
        EXCHANGE,
        INTERVAL,
        inputs={"length": 20},
        deployment=next(iter(settings_store.all_run_configs())),
        source_hash=new["source"]["hash"],
        input_fingerprints=fingerprints_of(new),
    )[0]

    seen = {}

    def spawn(
        script, run_id, symbol, exchange, interval, product, user_id, days, poll, inputs=None
    ):
        seen["script"] = script
        seen["inputs"] = inputs
        return True, "started"

    monkeypatch.setattr(service, "_spawn_claimed", spawn)
    ok, _ = service.start_run(SCRIPT)
    assert ok
    assert seen["inputs"] == {"length": 20}


def test_an_unchanged_hash_is_the_fast_path(monkeypatch):
    old = program("sha256:v1", number_input("length"))
    assert save_deployment(old, {"length": 20})[0]
    monkeypatch.setattr(service, "_spawn_claimed", lambda *a, **k: (True, "started"))
    assert service.start_run(SCRIPT)[0]


def test_the_gate_defers_when_the_program_cannot_be_read():
    # The service never owned "is there a program": the start route refuses one
    # before the service runs, and a restored run hands it to the child. The
    # input gate refuses only on positive evidence, so here it stands aside.
    settings_store.write_run_config(
        SCRIPT, SYMBOL, EXCHANGE, INTERVAL, source_hash="", input_fingerprints={}
    )
    assert review.start_refusal(settings_store.read_run_config(SCRIPT)) is None


# ---------------------------------------------------------------------------
# The routes: review first, confirm second, one write
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(utils.session, "is_session_valid", lambda: True)
    application = Flask(__name__)
    application.config["TESTING"] = True
    application.secret_key = "test-only"
    application.register_blueprint(openscript_bp)
    application.register_blueprint(openscript_runner_bp)
    return application.test_client()


def body(**over):
    held = {"symbol": SYMBOL, "exchange": EXCHANGE, "interval": INTERVAL, "inputs": {}}
    held.update(over)
    return held


def deployment_id_of():
    return next(iter(settings_store.all_run_configs()))


def test_the_first_save_reports_and_writes_nothing(client):
    old = program(
        "sha256:v1",
        number_input("length", 14, 1, 100),
        string_input("mode", "fast", ["fast", "slow"]),
        number_input("legacy", 1, 0, 10),
    )
    assert save_deployment(old, {"length": 20, "mode": "fast", "legacy": 1})[0]
    before = settings_store.CONFIG_FILE.read_text(encoding="utf-8")

    # length is untouched (kept); mode lost a choice (re-enter); legacy is gone
    # (follows the script's new text).
    new = program(
        "sha256:v2",
        number_input("length", 14, 1, 100),
        string_input("mode", "fast", ["fast"]),
    )
    openscript_store.program_path(openscript_store.script_dir(), SCRIPT).write_text(
        json.dumps(new), encoding="utf-8"
    )

    answer = client.post(
        f"/openscript/runner/config/{SCRIPT}",
        json=body(
            deployment=deployment_id_of(),
            inputs={"length": 20, "mode": "fast", "legacy": 1},
        ),
    )
    assert answer.status_code == 409
    payload = answer.get_json()
    assert payload["status"] == "confirm"
    listed = payload["review"]
    assert [one["key"] for one in listed["keep"]] == ["length"]
    assert [one["key"] for one in listed["reset"]] == ["mode"]
    assert [one["key"] for one in listed["default"]] == ["legacy"]

    # Nothing was written: the old settings are byte for byte what they were.
    assert settings_store.CONFIG_FILE.read_text(encoding="utf-8") == before
    assert settings_store.read_run_config(SCRIPT)["source_hash"] == "sha256:v1"


def test_the_confirmed_save_writes_once_with_cleaned_values(client):
    old = program(
        "sha256:v1",
        number_input("length", 14, 1, 100),
        string_input("mode", "fast", ["fast", "slow"]),
        number_input("legacy", 1, 0, 10),
    )
    assert save_deployment(old, {"length": 20, "mode": "fast", "legacy": 1})[0]
    new = program(
        "sha256:v2",
        number_input("length", 14, 1, 100),
        string_input("mode", "fast", ["fast"]),
    )
    openscript_store.program_path(openscript_store.script_dir(), SCRIPT).write_text(
        json.dumps(new), encoding="utf-8"
    )

    answer = client.post(
        f"/openscript/runner/config/{SCRIPT}",
        json=body(
            deployment=deployment_id_of(),
            inputs={"length": 20, "mode": "fast"},
            confirm=True,
        ),
    )
    assert answer.status_code == 200, answer.get_json()
    held = settings_store.read_run_config(SCRIPT)
    # The removed value was dropped; everything kept was still valid.
    assert held["inputs"] == {"length": 20, "mode": "fast"}
    assert held["source_hash"] == "sha256:v2"
    assert held["input_fingerprints"] == fingerprints_of(new)


def test_a_confirmation_that_still_carries_a_rejected_value_is_refused(client):
    old = program("sha256:v1", string_input("mode", "fast", ["fast", "slow"]))
    assert save_deployment(old, {"mode": "slow"})[0]
    new = program("sha256:v2", string_input("mode", "fast", ["fast"]))
    openscript_store.program_path(openscript_store.script_dir(), SCRIPT).write_text(
        json.dumps(new), encoding="utf-8"
    )

    answer = client.post(
        f"/openscript/runner/config/{SCRIPT}",
        json=body(deployment=deployment_id_of(), inputs={"mode": "slow"}, confirm=True),
    )
    assert answer.status_code == 400
    assert "mode" in answer.get_json()["message"]
    # The refusal leaves the old settings, old hash included, in place.
    assert settings_store.read_run_config(SCRIPT)["source_hash"] == "sha256:v1"


def test_a_save_with_nothing_to_review_writes_directly(client):
    new = program("sha256:v1", number_input("length", 14, 1, 100))
    openscript_store.program_path(openscript_store.script_dir(), SCRIPT).write_text(
        json.dumps(new), encoding="utf-8"
    )
    answer = client.post(f"/openscript/runner/config/{SCRIPT}", json=body(inputs={"length": 20}))
    assert answer.status_code == 200
    held = settings_store.read_run_config(SCRIPT)
    assert held["source_hash"] == "sha256:v1"
    assert held["inputs"] == {"length": 20}


def test_the_review_route_reports_the_stored_settings(client):
    old = program("sha256:v1", number_input("length", 14, 1, 100))
    assert save_deployment(old, {"length": 20})[0]
    new = program("sha256:v2", number_input("length", 14, 1, 500))
    openscript_store.program_path(openscript_store.script_dir(), SCRIPT).write_text(
        json.dumps(new), encoding="utf-8"
    )

    answer = client.get(f"/openscript/runner/config/{deployment_id_of()}/review")
    assert answer.status_code == 200
    payload = answer.get_json()["review"]
    assert payload["script"] == SCRIPT
    assert payload["source_hash"] == "sha256:v2"
    assert payload["saved_hash"] == "sha256:v1"
    assert payload["reset"][0]["reason"] == "changed"


def test_the_review_route_does_not_swallow_a_settings_or_program_that_is_absent(client):
    answer = client.get("/openscript/runner/config/none.oscript/review")
    assert answer.status_code == 404

    settings_store.write_run_config(
        SCRIPT, SYMBOL, EXCHANGE, INTERVAL, source_hash="", input_fingerprints={}
    )
    answer = client.get(f"/openscript/runner/config/{SCRIPT}/review")
    assert answer.status_code == 409


def test_confirm_is_an_allowed_field_and_an_unknown_one_is_still_refused(client):
    new = program("sha256:v1")
    openscript_store.program_path(openscript_store.script_dir(), SCRIPT).write_text(
        json.dumps(new), encoding="utf-8"
    )
    answer = client.post(
        f"/openscript/runner/config/{SCRIPT}",
        json=body(confirm=True, live=True),
    )
    assert answer.status_code == 400
    assert "live" in answer.get_json()["message"]
