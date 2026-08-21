"""The e2e host's own app — it answers the one Function moderation calls out to.

``stapel-moderation`` depends on an ANSWERER for ``llm.complete``, not on
``stapel-agent`` itself: the model is reached by string name over comm, and
that indirection is the module's central claim about its screener. Registering
a scripted provider here rather than installing the agent keeps this run a
proof of THIS module — it exercises the real call path and the real envelope
handling, and it does not go red because a neighbour's provider is
rate-limited or mid-migration.

The provider is scripted from a file the run writes, so the script can say
"the model rejects this one" and "the model is down now" without restarting
the host. That second case is the important one: it is how the run proves the
failure envelope becomes a retry rather than a silent success.

The notification sink does the same job for ``request_notification``: it
appends every request to a JSONL file the run reads, so "the author was told,
with a reason and an appeal link" is an assertion rather than a hope.
"""
import json
import os
from pathlib import Path

from django.apps import AppConfig

STATE_DIR = Path(
    os.environ.get("STAPEL_MODERATION_E2E_DIR", "/tmp/stapel-moderation-e2e")
)
SCRIPT_FILE = STATE_DIR / "llm_script.json"
NOTIFY_FILE = STATE_DIR / "notifications.jsonl"


class E2EConfig(AppConfig):
    name = "e2e"
    label = "e2e_host"

    def ready(self):
        from stapel_core.comm import register_function

        register_function("llm.complete", _complete)
        # stapel-listings asks the category registry for a category's feature
        # schema on publish. This host has no stapel-categories, and does not
        # need one: the seed listing carries no features, so an empty schema
        # is the truthful answer rather than a stub standing in for something.
        register_function("categories.features", lambda payload: {"configs": []})
        _capture_notifications()
        _make_celery_eager()


def _complete(payload):
    """Answer whatever the run's script file currently says.

    A missing script file is an approval, which keeps any unscripted screening
    out of the way of the step under test.
    """
    try:
        script = json.loads(SCRIPT_FILE.read_text())
    except (OSError, ValueError):
        script = {}
    if script.get("status") == "failure":
        # The shape that matters: llm.complete never raises for a provider,
        # it returns this. The handler must turn it into a raise, or the task
        # is marked DONE and the retry ladder never runs.
        return {"status": "failure", "reason": script.get("reason", "provider down")}
    return {
        "status": "ok",
        "result": {
            "decision": script.get("decision", "approved"),
            "reason_code": script.get("reason_code", ""),
            "rationale": script.get("rationale", "Scripted e2e verdict."),
            "confidence": script.get("confidence", 0.95),
        },
        "model": "e2e-scripted",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _capture_notifications():
    """Record every request_notification call to a file the run can read.

    Patched rather than mocked at the call site: the module must go through
    the real ``stapel_core.notifications`` entry point, because the thing
    being proved is that IT requests the right type with the right variables.
    """
    from stapel_core import notifications as core_notifications
    from stapel_core.notifications import publish as core_publish

    def _sink(notification_type, **kwargs):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with NOTIFY_FILE.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"type": notification_type, **_jsonable(kwargs)}) + "\n"
            )
        return True

    core_publish.request_notification = _sink
    core_notifications.request_notification = _sink


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _make_celery_eager():
    """One process, no broker."""
    try:
        from celery import current_app
    except ImportError:
        return
    current_app.conf.task_always_eager = True
    current_app.conf.task_eager_propagates = True
