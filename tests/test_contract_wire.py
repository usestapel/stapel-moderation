"""Every response body the contract declares is a body the views actually send.

``docs/schema.json`` is emitted from the views' ``@extend_schema``
annotations, and an annotation is a CLAIM: it says what the view returns, and
the generator has no way to check it against the method body.
``tests/test_contract.py`` compares the committed document against a FRESH
EMISSION of the same annotations — it proves the file is not stale, and
nothing else, because both sides come from the claim. stapel-alerts 0.2.0
shipped ``GET /issues`` declared as ``Issue[]`` while the wire carried
``{count, offset, limit, results}``: the drift gate was green and the
frontend pair rendered ``undefined``.

This is the gate the generator cannot be: it performs every operation the
committed schema declares with a JSON response body, and validates the body
it gets against the schema it was promised.

Rules this file holds itself to:

* an operation with a declared JSON response and no entry in ``RECIPES``
  FAILS LOUDLY — a gate that quietly covers seventeen of eighteen rows is the
  family of green that proves nothing;
* a path parameter the gate cannot fill fails at the point of substitution,
  naming the operation;
* the operations that genuinely cannot be driven in-process are listed by
  name in ``UNDRIVABLE`` with a one-line reason each. That list is asserted
  to be exactly current: a stale entry, or a missing reason, fails;
* a collection that comes back empty fails in the populated pass — an empty
  array validates against any item schema, so an empty answer is a check that
  looked at nothing;
* a body is checked in BOTH directions. ``jsonschema`` answers "is every
  declared property satisfied"; an OpenAPI object schema without
  ``additionalProperties`` also claims to ENUMERATE the body, and a key the
  document never mentions is a key no generated client has a field for. That
  half is :func:`_undeclared_keys`;
* every read is driven a SECOND time in its emptiest legal state
  (``EMPTY_STATE``). Every null finding in the first wave of this gate was
  there: an ``exp`` null for every active token, a ``created_at`` null for
  every account that had just signed up, a counter null for every account
  without the feature.

Runs on every interpreter: it reads the committed schema and never emits.

THE MOUNT. ``codegen_urls.py`` mounts ``moderation/`` and the module's own
``urls.py`` contributes ``api/v1/``, so the document is written against
``/moderation/api/v1/…``. ``tests/urls.py`` mounts the same thing, so this
module is one of the ones whose suite was already looking where its document
describes — five of the first eight libraries in this wave were not. The
emission mount is declared here anyway, because
``test_every_declared_path_resolves_under_this_urlconf`` can only hold if the
urlconf under test is this file's own.

What it found on its first run: 18 of 18 operations driven, both states, one
defect reaching two of them.

* ``VerdictPresenterDTO.confidence`` is declared a REQUIRED, non-nullable
  ``number`` and is ``null`` on every HUMAN verdict — which is every verdict
  ``POST /cases/{case_id}/verdict`` can produce, and every verdict row on the
  card of a case a moderator has actually worked. The column is null by
  design (``models.py:425``, "LLM only."); what is wrong is the claim.
  ``VerdictPresenter`` lists ``confidence`` among its as-is ``fields``
  (``presenters.py:222``) and ``_infer_type`` in stapel-core maps
  ``models.FloatField`` to ``float`` without reading ``null=True``, so the
  generated DTO says ``float`` where the model says ``float | None``. The
  presenter already spells this correctly for ``actor_id`` two lines below,
  with ``Optional[str]`` in ``custom_fields``. Recorded in
  ``KNOWN_MISMATCHES``, left exactly as it is: this is a gate, not a fix.

Everything else holds, in both states, in both directions: no operation here
answers a key the contract fails to mention either (the check that caught
stapel-video's ``lobby/deny`` runs on all eighteen of these bodies and finds
nothing). And
``test_the_gate_is_not_blind`` proves that is a finding rather than a gate
that never looked: it re-drives every honest operation with its declared
schema swapped for ``{"type": "string"}`` and requires all of them to fail.
"""
import copy
import json
import re
import uuid
from pathlib import Path

import jsonschema
import pytest
from django.test import override_settings
from django.urls import include, path as url_path
from rest_framework.test import APIClient

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "docs" / "schema.json").read_text())

#: The mount the contract is emitted at (``codegen_urls.py``), reproduced for
#: the test client rather than borrowed from ``tests/urls.py``: a gate that
#: inherits the suite's mount cannot notice when the suite's mount is wrong.
urlpatterns = [
    url_path("moderation/", include("stapel_moderation.urls")),
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]

V1 = "/moderation/api/v1"

#: The two roles this module's own harness documents for hosts: MID reads the
#: console, HIGH mutates it. Declared for the whole file because every
#: moderator recipe needs one of them and ``@requires`` is resolved per
#: request, not per client.
ACCESS_ROLES = {
    "ROLES": {
        "moderator": {"clearance": "low", "apps": {"moderation": "mid"}},
        "ts_lead": {"clearance": "mid", "apps": {"moderation": "high"}},
    }
}


@pytest.fixture(autouse=True)
def _mandate_roles():
    with override_settings(STAPEL_ACCESS=ACCESS_ROLES):
        yield


@pytest.fixture(autouse=True)
def _media_root(tmp_path):
    """Nothing here writes files today; pin the root so nothing ever does.

    ``MEDIA_ROOT`` is unset in this module's harness settings, so it defaults
    to the working directory — in stapel-auth that put a data export into the
    checkout, where a stray ``gdpr/`` directory then shadowed a real module.
    A single future upload on the evidence path would land the same way.
    """
    with override_settings(MEDIA_ROOT=str(tmp_path)):
        yield


# ─────────────────────────────────────────────────────────────────────────────
# The contract side: what the document declares
# ─────────────────────────────────────────────────────────────────────────────


def _json_schema(node):
    """OpenAPI 3.0 → JSON Schema, for the divergences that matter here.

    OAS 3.0 spells "may be null" as ``nullable: true`` beside a ``type`` (or
    beside an ``allOf`` wrapping a ``$ref``); JSON Schema has no such keyword
    and would refuse the null — which is exactly what most of these fields
    answer in their empty state, and the whole reason the empty pass exists.
    Everything else drf-spectacular emits here (``$ref``, ``allOf``,
    ``required``, ``additionalProperties``) is JSON Schema as written.
    """
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _validator(response_schema):
    root = copy.deepcopy(response_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    return jsonschema.Draft202012Validator(_json_schema(root))


def _operations():
    """Every ``(method, path, 2xx code, JSON body schema)`` the contract declares."""
    ops = []
    for path, methods in SCHEMA["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for code, response in op.get("responses", {}).items():
                body = (
                    response.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if body is not None and code.startswith("2"):
                    ops.append((method.upper(), path, int(code), body))
    return sorted(ops, key=lambda o: (o[1], o[0], o[2]))


OPERATIONS = _operations()


# ─────────────────────────────────────────────────────────────────────────────
# The second half of the check: keys the document never mentions
# ─────────────────────────────────────────────────────────────────────────────


def _deref(node):
    """Follow one ``$ref`` into ``components.schemas``."""
    ref = node.get("$ref") if isinstance(node, dict) else None
    if not ref:
        return node
    name = ref.rsplit("/", 1)[-1]
    return SCHEMA["components"]["schemas"].get(name, {})


def _properties_of(node):
    """``(properties, enumerates)`` for one object schema.

    ``enumerates`` is False when the schema declines to be a closed list —
    it has an ``additionalProperties`` of its own (a free-form map: this
    module uses several for settings, payload and answer blobs) or it is a
    ``oneOf``/``anyOf`` this walk will not try to choose between. Those are
    skipped rather than guessed at: a false positive here would be worse than
    the miss, because it would teach the next reader to distrust the check.
    """
    node = _deref(node)
    if not isinstance(node, dict):
        return {}, False
    if "oneOf" in node or "anyOf" in node:
        return {}, False
    if "additionalProperties" in node:
        return dict(node.get("properties") or {}), False
    properties = dict(node.get("properties") or {})
    for branch in node.get("allOf") or ():
        branch_properties, branch_enumerates = _properties_of(branch)
        properties.update(branch_properties)
        if not branch_enumerates:
            return properties, False
    if not properties:
        return {}, False
    return properties, True


def _undeclared_keys(body, schema, path=()):
    """Keys the received body carries that the declared schema never names.

    ``jsonschema`` answers one half of "does this body match the contract":
    every declared property is there and well typed. The other half is that
    the contract ENUMERATES the body — a client is generated from the
    document, so a key the document does not mention is a key no generated
    type has a field for, and reading it is ``undefined`` at runtime and a
    compile error in a typed client. An OpenAPI schema with ``properties``
    and no ``additionalProperties`` is exactly that claim, and this is what
    checks it. It is what caught ``POST /rooms/{join_code}/lobby/deny`` in
    stapel-video: a body carrying a ``status`` key the document never
    mentions, which plain validation passes without a word.
    """
    found = []
    if isinstance(body, list):
        items = _deref(schema).get("items") if isinstance(_deref(schema), dict) else None
        if items is not None:
            for index, item in enumerate(body):
                found.extend(_undeclared_keys(item, items, path + (index,)))
        return found
    if not isinstance(body, dict):
        return found
    properties, enumerates = _properties_of(schema)
    if enumerates:
        for key in body:
            if key not in properties:
                found.append(".".join(str(part) for part in path + (key,)))
    for key, value in body.items():
        if key in properties:
            found.extend(_undeclared_keys(value, properties[key], path + (key,)))
    return found


# ─────────────────────────────────────────────────────────────────────────────
# The wire side: harness
# ─────────────────────────────────────────────────────────────────────────────


def _unique(prefix):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def anonymous():
    return APIClient()


def make_user(**kwargs):
    from django.contrib.auth import get_user_model

    defaults = dict(
        username=_unique("wire-"),
        email=f"{_unique('wire-')}@example.com",
        password="wire-contract-password-7",
    )
    defaults.update(kwargs)
    return get_user_model().objects.create_user(**defaults)


def make_lead():
    """HIGH clearance in the moderation app — claim, verdict, sanction, appeal."""
    user = make_user(is_staff=True)
    user.staff_roles = ["ts_lead"]
    return user


def client_for(user):
    """A FRESH client per actor.

    Not a convenience: the appeal recipes act as three people in one scenario
    (the author who appealed, the moderator who decided, the second moderator
    who hears it), and re-authenticating one shared client silently changes
    who every earlier handle is.
    """
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def lead_client():
    return client_for(make_lead())


def provide(name, handler):
    """Register one comm Function provider, displacing whatever holds the name.

    ``FunctionRegistry.register`` refuses a second provider for a name, and
    ``test_the_gate_is_not_blind`` drives all eighteen recipes inside ONE
    test — so a plain ``@function`` double would raise on the second recipe
    that needs the same seam. The conftest's snapshot/restore fixture puts
    the module's own providers back afterwards.
    """
    from stapel_core.comm.registry import function_registry

    function_registry._providers.pop(name, None)
    function_registry._schemas.pop(name, None)
    function_registry.register(name, handler)


def content_seam(author_user=None, key=None):
    """A ``listing`` target type backed by an in-process content Function.

    The double is the seam itself: the module never imports a target module,
    it calls a name. Returns the KEY it answers for — unique per call, because
    a case is unique per (target_type, target_key) among the open states and
    ``test_the_gate_is_not_blind`` drives all eighteen recipes inside ONE
    test: a shared key means the second recipe finds the first one's case,
    already claimed, and ``start_screening`` refuses the transition.
    """
    key = key or _unique("listing-")
    from stapel_moderation.registry import register_target_type

    state = {
        "listing_id": key,
        "title": "A bicycle",
        "text": "Barely used, good condition.",
        "language": "en",
        "media": [],
        "author_id": str(author_user.pk) if author_user is not None else "",
        "url": f"https://example.test/listings/{key}",
        "status": "published",
        "moderation_status": "pending",
    }

    def _content(payload):
        if str(payload.get("listing_id")) != str(state["listing_id"]):
            raise LookupError(f"listing {payload.get('listing_id')} not found")
        return dict(state)

    provide("listings.moderation_content", _content)
    register_target_type(
        "listing",
        {
            "intake_events": ["listing.submitted"],
            "id_field": "listing_id",
            "content_function": "listings.moderation_content",
            "verdict_event": "moderation.completed",
            "notification_types": {"content_blocked": "listing_blocked"},
        },
    )
    return key


def llm_seam(decision="needs_review"):
    """An ``llm.complete`` double answering the envelope a real provider does."""
    envelope = {
        "status": "ok",
        "result": {
            "decision": decision,
            "reason_code": "",
            "rationale": "Nothing objectionable.",
            "confidence": 0.95,
        },
        "model": "medium",
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
    provide("llm.complete", lambda payload: envelope)
    return envelope


def queued_case(key, subject=None):
    """A case parked in the human queue — the state the console reads.

    The screener is driven for real (``TASK_DISPATCH: inline`` in the harness
    settings) and abstains, which is how a case reaches ``queued`` in a
    deployment rather than by writing the row directly.
    """
    from stapel_core.comm import mutate_and_emit

    from stapel_moderation import services

    llm_seam("needs_review")
    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing",
            key,
            origin="submission",
            subject_user_id=subject.pk if subject is not None else None,
            emit_event=emit_event,
        )
        services.start_screening(case, emit_event=emit_event)
    case.refresh_from_db()
    return case


def bare_case(key=None):
    """A case with nothing on it: no reports, no verdicts, no subject, no
    content function, no screening. The emptiest legal card."""
    from stapel_core.comm import mutate_and_emit

    from stapel_moderation import services

    with mutate_and_emit() as emit_event:
        case, _ = services.open_case(
            "listing",
            key or _unique("empty-"),
            origin="manual",
            emit_event=emit_event,
        )
    case.refresh_from_db()
    return case


def resolved_case(key, author, decider=None):
    """A case a moderator has decided — the precondition for an appeal."""
    case = queued_case(key, subject=author)
    decider = decider or make_lead()
    response = client_for(decider).post(
        f"{V1}/cases/{case.id}/verdict", {"decision": "rejected"}, format="json"
    )
    assert response.status_code == 201, response.content
    case.refresh_from_db()
    return case, decider


def open_appeal(author, case):
    response = client_for(author).post(
        f"{V1}/appeals/",
        {"case_id": str(case.id), "body": "Here is the certificate."},
        format="json",
    )
    assert response.status_code == 201, response.content
    return response.json()["id"]


def issue_sanction(subject, *, actor=None):
    client = client_for(actor or make_lead())
    response = client.post(
        f"{V1}/sanctions",
        {
            "subject_user_id": str(subject.pk),
            "kind": "suspended",
            "reason_code": "fraud",
            "duration_seconds": 3600,
        },
        format="json",
    )
    assert response.status_code == 201, response.content
    return response.json()["id"]


# ─────────────────────────────────────────────────────────────────────────────
# The recipe table
# ─────────────────────────────────────────────────────────────────────────────


class Call:
    """Performs one declared operation, and refuses to guess a path parameter."""

    def __init__(self, method, path):
        self.method = method
        self.path = path

    def __call__(self, client, params=None, data=None, query="", **extra):
        url = self.path
        for name, value in (params or {}).items():
            url = url.replace("{%s}" % name, str(value))
        assert "{" not in url, (
            f"{self.method} {self.path}: a path parameter this gate does not "
            "know how to fill — teach its recipe, or the operation goes unchecked"
        )
        send = getattr(client, self.method.lower())
        if self.method in ("GET", "DELETE"):
            return send(url + query, **extra)
        return send(url + query, data if data is not None else {}, format="json", **extra)


#: How to perform each operation the contract declares with a JSON response
#: body, keyed by ``(METHOD, path template, status code)``. ``code`` is
#: ``None`` for the usual case of one 2xx per operation.
RECIPES = {}

#: The same operations again, in the emptiest state the contract still has to
#: describe: no rows, or the one row the operation addresses carrying none of
#: its optional values. A populated answer cannot say what a field holds when
#: there is nothing to hold, and that is where every null finding in the first
#: wave of this gate was.
EMPTY_STATE = {}


def recipe(method, path, code=None, table=None):
    def register(fn):
        target = RECIPES if table is None else table
        key = (method, V1 + path, code)
        assert key not in target, f"duplicate recipe for {method} {path} {code}"
        target[key] = fn
        return fn

    return register


def empty_state(method, path, code=None):
    return recipe(method, path, code, table=EMPTY_STATE)


#: Operations that cannot be driven in-process, by name and with the reason.
#:
#: EMPTY. Every operation this module declares is reachable from a test
#: client, including the ones behind a content read and an LLM screening —
#: those are comm Function SEAMS a deployment wires, so the gate wires them
#: too and everything on this side of the seam runs for real.
UNDRIVABLE: dict = {}


# ── intake: reports ──────────────────────────────────────────────────────────


@recipe("POST", "/reports/")
def _report_create(call):
    author = make_user()
    key = content_seam(author)
    llm_seam("needs_review")
    return call(
        client_for(make_user()),
        data={
            "target_type": "listing",
            "target_key": key,
            "reason_code": "spam",
            "good_faith": True,
        },
    )


@recipe("GET", "/reports/")
def _report_list(call):
    author = make_user()
    key = content_seam(author)
    llm_seam("needs_review")
    reporter = make_user()
    client = client_for(reporter)
    filed = client.post(
        f"{V1}/reports/",
        {"target_type": "listing", "target_key": key, "reason_code": "spam"},
        format="json",
    )
    assert filed.status_code == 201, filed.content
    return call(client)


@empty_state("GET", "/reports/")
def _report_list_empty(call):
    """Somebody who has never complained about anything."""
    return call(client_for(make_user()))


# ── intake: the public disclosure ────────────────────────────────────────────


@recipe("GET", "/policy")
def _policy(call):
    content_seam(make_user())
    return call(anonymous())


@empty_state("GET", "/policy")
def _policy_empty(call):
    """No target types registered at all — the disclosure a fresh deployment
    serves before a host has declared what it moderates."""
    return call(anonymous())


# ── intake: appeals ──────────────────────────────────────────────────────────


@recipe("POST", "/appeals/")
def _appeal_create(call):
    author = make_user()
    key = content_seam(author)
    case, _decider = resolved_case(key, author)
    return call(
        client_for(author),
        data={"case_id": str(case.id), "body": "Here is the certificate."},
    )


@recipe("GET", "/appeals/")
def _appeal_list(call):
    author = make_user()
    key = content_seam(author)
    case, _decider = resolved_case(key, author)
    open_appeal(author, case)
    return call(client_for(author))


@empty_state("GET", "/appeals/")
def _appeal_list_empty(call):
    return call(client_for(make_user()))


# ── console: the queue ───────────────────────────────────────────────────────


@recipe("GET", "/cases")
def _case_list(call):
    author = make_user()
    key = content_seam(author)
    queued_case(key, subject=author)
    return call(lead_client())


@empty_state("GET", "/cases")
def _case_list_empty(call):
    return call(lead_client())


@recipe("GET", "/cases/{case_id}")
def _case_detail(call):
    """A fully worked card: reports, verdicts, sanctions and appeals all non-empty.

    A card built out of a freshly queued case would leave four declared arrays
    empty, and an empty array validates against any item schema — so the
    recipe works the case to the end instead, which is also the state a
    console actually reads.
    """
    author = make_user()
    key = content_seam(author)
    case = queued_case(key, subject=author)

    # A complaint on the same target, so the card carries a report row.
    reporter = client_for(make_user())
    assert reporter.post(
        f"{V1}/reports/",
        {"target_type": "listing", "target_key": key, "reason_code": "spam"},
        format="json",
    ).status_code == 201

    decided = lead_client().post(
        f"{V1}/cases/{case.id}/verdict",
        {
            "decision": "rejected",
            "reason_code": "counterfeit",
            "sanction": {"kind": "posting_restricted", "duration_seconds": 3600},
        },
        format="json",
    )
    assert decided.status_code == 201, decided.content
    open_appeal(author, case)

    return call(lead_client(), params={"case_id": case.id})


@empty_state("GET", "/cases/{case_id}")
def _case_detail_empty(call):
    """The emptiest card the contract still has to describe.

    No subject, no reports, no verdicts, no sanctions, no appeals, and no
    content function registered — so ``content`` is the ``no_content_function``
    branch rather than a read that succeeded. Every nullable field on
    ``CaseDetailPresenterDTO`` answers null here, which is the state the first
    wave of this gate found every one of its lies in.
    """
    return call(lead_client(), params={"case_id": bare_case().id})


@recipe("POST", "/cases/{case_id}/claim")
def _case_claim(call):
    author = make_user()
    key = content_seam(author)
    case = queued_case(key, subject=author)
    return call(lead_client(), params={"case_id": case.id})


@recipe("POST", "/cases/{case_id}/release")
def _case_release(call):
    author = make_user()
    key = content_seam(author)
    case = queued_case(key, subject=author)
    lead = make_lead()
    client = client_for(lead)
    assert client.post(f"{V1}/cases/{case.id}/claim").status_code == 200
    return call(client, params={"case_id": case.id})


@recipe("POST", "/cases/{case_id}/verdict")
def _case_verdict(call):
    author = make_user()
    key = content_seam(author)
    case = queued_case(key, subject=author)
    return call(
        lead_client(),
        params={"case_id": case.id},
        data={
            "decision": "rejected",
            "reason_code": "counterfeit",
            "note": "Replica.",
            "sanction": {"kind": "posting_restricted", "duration_seconds": 3600},
        },
    )


@recipe("POST", "/cases/{case_id}/rescan")
def _case_rescan(call):
    author = make_user()
    key = content_seam(author)
    case, _decider = resolved_case(key, author)
    return call(lead_client(), params={"case_id": case.id})


@recipe("GET", "/cases/{case_id}/events")
def _case_events(call):
    author = make_user()
    key = content_seam(author)
    case = queued_case(key, subject=author)
    return call(lead_client(), params={"case_id": case.id})


@empty_state("GET", "/cases/{case_id}/events")
def _case_events_empty(call):
    """A case nobody has touched: one ``created`` row, no actor on it.

    ``actor_id`` is declared nullable and this is the state that answers null
    — the system opened the case, no human did.
    """
    return call(lead_client(), params={"case_id": bare_case().id})


@recipe("GET", "/stats")
def _stats(call):
    author = make_user()
    key = content_seam(author)
    queued_case(key, subject=author)
    return call(lead_client())


@empty_state("GET", "/stats")
def _stats_empty(call):
    """No cases at all — every counter zero and every group-by empty."""
    return call(lead_client())


# ── console: sanctions ───────────────────────────────────────────────────────


@recipe("POST", "/sanctions")
def _sanction_create(call):
    content_seam(make_user())
    return call(
        lead_client(),
        data={
            "subject_user_id": str(make_user().pk),
            "kind": "suspended",
            "reason_code": "fraud",
            "duration_seconds": 3600,
        },
    )


@recipe("GET", "/sanctions")
def _sanction_list(call):
    content_seam(make_user())
    issue_sanction(make_user())
    return call(lead_client())


@empty_state("GET", "/sanctions")
def _sanction_list_empty(call):
    return call(lead_client())


@recipe("POST", "/sanctions/{sanction_id}/lift")
def _sanction_lift(call):
    content_seam(make_user())
    sanction_id = issue_sanction(make_user())
    return call(
        lead_client(), params={"sanction_id": sanction_id}, data={"note": "Overcorrected."}
    )


# ── console: appeals ─────────────────────────────────────────────────────────


@recipe("GET", "/appeals/queue")
def _appeal_queue(call):
    author = make_user()
    key = content_seam(author)
    case, _decider = resolved_case(key, author)
    open_appeal(author, case)
    return call(lead_client())


@empty_state("GET", "/appeals/queue")
def _appeal_queue_empty(call):
    return call(lead_client())


@recipe("POST", "/appeals/{appeal_id}/resolve")
def _appeal_resolve(call):
    """DSA Art. 20 independence: the second pair of eyes, not the first.

    The moderator who decided the case is refused (``same_actor``), so the
    recipe has to mint a different lead — which is the deployment's own rule,
    not a convenience of the gate.
    """
    author = make_user()
    key = content_seam(author)
    case, decider = resolved_case(key, author)
    appeal_id = open_appeal(author, case)
    reviewer = make_lead()
    assert reviewer.pk != decider.pk
    return call(
        client_for(reviewer),
        params={"appeal_id": appeal_id},
        data={"outcome": "overturned", "note": "Certificate checks out."},
    )


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────


#: Operations whose declared body the wire does not send. Each entry names the
#: defect AND its owner, and ``strict=True`` turns a fixed one into a failure
#: until the entry is deleted — so a finding can be neither forgotten nor
#: quietly kept. Recorded, not fixed: this is a gate.
KNOWN_MISMATCHES: dict = {
    ("POST", V1 + "/cases/{case_id}/verdict"): (
        "declares VerdictPresenterDTO.confidence as a REQUIRED, non-nullable "
        "`number` and answers null for every HUMAN verdict — which is every "
        "verdict this endpoint can produce. Owner: stapel-moderation, "
        "presenters.py:222, where VerdictPresenter lists `confidence` among "
        "the as-is `fields`; stapel_core.django.api.presenters._infer_type "
        "maps models.FloatField -> float and does not read `null=True`, so "
        "the generated DTO field is `float`, not `Optional[float]`. The "
        "column is null by design (models.py:425, '#: LLM only.') and "
        "CaseVerdictView.post (views.py:515) calls services.resolve_case "
        "without a confidence, so services.py:901 leaves it None. The fix is "
        "one line in THIS module: move `confidence` into custom_fields as "
        "Optional[float], exactly as `actor_id` already is two lines below. "
        "A generated client reads verdict.confidence as a number and gets "
        "null on every decision a moderator makes."
    ),
    ("GET", V1 + "/cases/{case_id}"): (
        "same defect, reached through the card: CaseDetailPresenterDTO."
        "verdicts[] is VerdictPresenterDTO, so every worked case — one a "
        "moderator has actually decided — carries a verdict row whose "
        "REQUIRED `confidence` is null. Owner: stapel-moderation "
        "presenters.py:222 (see the verdict entry above). This is the "
        "operation the console reads, so the defect reaches a screen rather "
        "than only a write response."
    ),
}

#: Which pass each recorded mismatch applies to.
#:
#: A defect that shows in only ONE state must not xfail the other: with
#: ``strict=True`` an honest answer marked xfail is itself a failure, and
#: marking both passes would be a claim this gate has not made. ``GET
#: /cases/{case_id}`` is honest on an untouched case (no verdict rows at all)
#: and lies on a worked one. Anything not named here applies to both.
MISMATCH_STATES: dict = {
    ("GET", V1 + "/cases/{case_id}"): frozenset({"populated"}),
}

_ALL_STATES = frozenset({"populated", "empty"})


def _mismatch_reason(method, path, state):
    """The recorded reason if this operation lies in THIS state, else None."""
    key = (method, path)
    if key not in KNOWN_MISMATCHES:
        return None
    if state not in MISMATCH_STATES.get(key, _ALL_STATES):
        return None
    return KNOWN_MISMATCHES[key]


def _recipe_for(table, method, path, code):
    """The code-specific recipe if there is one, else the operation's."""
    return table.get((method, path, code)) or table.get((method, path, None))


def test_the_contract_declares_something_to_check():
    assert OPERATIONS, "docs/schema.json declares no JSON responses at all"


def test_every_declared_path_resolves_under_this_urlconf():
    """The suite must be looking where the document describes.

    Five of the first eight libraries this gate was written for had a
    committed contract that nothing had ever driven, because the test urlconf
    mounted somewhere the document does not describe: a prefix one segment
    short, the paths bare, less than the emission, both segments skipped, a
    doubled prefix. In every case the operations were "covered" by a file that
    could not have reached a single one of them.

    That is the same family as a gate nobody asks: the recipes can all be
    written, the run can be green, and not one request went where the contract
    says it goes. A missing recipe already fails loudly; this fails when the
    MOUNT is wrong, which no per-operation check can see, because when the
    mount is wrong every operation is equally and silently unreachable.

    Asserted against the urlconf THIS module declares — inheriting the suite's
    mount would be exactly the blindness the check exists to remove.
    """
    from django.urls import Resolver404, resolve

    # Resolution cares about the SHAPE of a segment, and this URL set uses
    # uuid converters. A path counts as reachable if any one shape resolves:
    # the question here is whether the mount exists, not whether an id does.
    candidates = (
        "00000000-0000-4000-8000-000000000000",
        "1",
        "a-slug",
    )

    unreachable = []
    for _method, path, _code, _schema in OPERATIONS:
        for value in candidates:
            try:
                resolve(re.sub(r"\{[^}]+\}", value, path))
                break
            except Resolver404:
                continue
        else:
            unreachable.append(path)

    assert not unreachable, (
        "these declared paths do not resolve under this module's urlconf, so "
        "nothing here can be driving them — the mount is wrong, not the "
        "recipes:\n  " + "\n  ".join(sorted(set(unreachable)))
    )


def test_every_declared_operation_is_driven_or_named_undrivable():
    """No operation is covered by silence, and no entry outlives its operation."""
    missing = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if _recipe_for(RECIPES, method, path, code) is None
        and (method, path) not in UNDRIVABLE
    ]
    assert not missing, (
        "operations with a declared JSON response body and no recipe:\n"
        + "\n".join(f"  {m} {p} -> {c}" for m, p, c in missing)
    )

    declared_codes = {(m, p, c) for m, p, c, _ in OPERATIONS}
    declared_ops = {(m, p) for m, p, _c, _ in OPERATIONS}
    stale = sorted(
        key
        for key in RECIPES
        if (key[0], key[1]) not in declared_ops
        or (key[2] is not None and key not in declared_codes)
    )
    assert not stale, (
        "recipes for operations/status codes the contract no longer declares:\n"
        + "\n".join(f"  {m} {p} -> {c}" for m, p, c in stale)
    )
    stale_exclusions = sorted(set(UNDRIVABLE) - declared_ops)
    assert not stale_exclusions, (
        f"exclusions for operations the contract no longer declares: {stale_exclusions}"
    )
    both = sorted((m, p) for m, p, _c in RECIPES if (m, p) in UNDRIVABLE)
    assert not both, f"driven AND excluded: {both}"
    for key, reason in UNDRIVABLE.items():
        assert reason and reason.strip(), f"{key} is excluded with no reason"

    # RECIPES ∪ UNDRIVABLE is EXACTLY the declared set — asserted as sets, so
    # neither an operation nobody drives nor an entry nobody needs survives.
    covered = {(m, p) for m, p, _c in RECIPES} | set(UNDRIVABLE)
    assert covered == declared_ops, (
        "RECIPES ∪ UNDRIVABLE is not the declared set:\n"
        f"  declared but uncovered: {sorted(declared_ops - covered)}\n"
        f"  covered but undeclared: {sorted(covered - declared_ops)}"
    )


def test_every_read_is_also_driven_in_its_emptiest_state():
    """A populated answer cannot say what a field holds when there is nothing.

    Every null finding in the first wave of this gate was on the empty state.
    A gate that only ever seeds three rows and asks never sees any of them.
    """
    exempt: set = set()
    reads = {
        (method, path)
        for method, path, _code, _schema in OPERATIONS
        if method == "GET"
    }
    covered = {(m, p) for m, p, _c in EMPTY_STATE}
    missing = sorted(reads - covered - exempt)
    assert not missing, (
        "reads driven only against a populated database — the state where "
        "every null claim in this gate's history was found is unchecked:\n"
        + "\n".join(f"  {m} {p}" for m, p in missing)
    )
    declared_ops = {(m, p) for m, p, _c, _ in OPERATIONS}
    stale = sorted({(m, p) for m, p, _c in EMPTY_STATE} - declared_ops)
    assert not stale, f"empty-state recipes for undeclared operations: {stale}"


def test_every_known_mismatch_is_still_declared_and_explained():
    """A recorded defect must name a live operation and carry its reason.

    Without this, an operation that is renamed or removed leaves an entry that
    silences nothing and reads like a known problem forever.
    """
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    for key, reason in KNOWN_MISMATCHES.items():
        assert key in declared, (
            f"{key} is recorded as a known mismatch but the contract no longer "
            "declares it — delete the entry"
        )
        assert reason and reason.strip(), f"{key} is recorded with no reason"

    stale_states = sorted(set(MISMATCH_STATES) - set(KNOWN_MISMATCHES))
    assert not stale_states, (
        f"MISMATCH_STATES narrows operations that are not recorded as "
        f"mismatches at all: {stale_states}"
    )
    for key, states in MISMATCH_STATES.items():
        assert states and states <= _ALL_STATES, (
            f"{key} is narrowed to {sorted(states)}, which is not a subset of "
            f"{sorted(_ALL_STATES)} — an empty or unknown set silences nothing"
        )


def _drive(table, method, path, code, body_schema, *, expect_rows):
    perform = _recipe_for(table, method, path, code)
    assert perform is not None, (
        f"{method} {path} declares a response body and has no recipe — an "
        "unchecked operation is a schema nobody proves. Teach RECIPES, or "
        "name it in UNDRIVABLE with a reason."
    )

    response = perform(Call(method, path))
    assert response.status_code == code, (
        f"{method} {path}: expected the declared {code}, got "
        f"{response.status_code}: {response.content[:400]}"
    )

    body = response.json()
    errors = sorted(_validator(body_schema).iter_errors(body), key=lambda e: list(e.path))
    assert not errors, (
        f"{method} {path} answers a body the contract does not describe:\n"
        + "\n".join(f"  at {list(e.path) or '<root>'}: {e.message}" for e in errors[:10])
        + f"\n  body: {json.dumps(body)[:600]}"
    )
    # The other direction: a key the document never mentions is a key no
    # generated client has a field for.
    undeclared = _undeclared_keys(body, body_schema)
    assert not undeclared, (
        f"{method} {path} answers keys the contract never mentions, so no "
        f"generated client has a field for them: {sorted(undeclared)}"
        + f"\n  body: {json.dumps(body)[:600]}"
    )
    # An empty list validates against any item schema, so a collection must
    # actually carry a row for the check to have looked at anything.
    if expect_rows and isinstance(body, list):
        assert body, f"{method} {path}: the declared collection came back empty"
    return body


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    OPERATIONS,
    ids=[f"{m} {p} {c}" for m, p, c, _ in OPERATIONS],
)
def test_the_wire_matches_the_declared_response(method, path, code, body_schema, request):
    if (method, path) in UNDRIVABLE:
        pytest.skip(f"excluded by name: {UNDRIVABLE[(method, path)]}")

    reason = _mismatch_reason(method, path, "populated")
    if reason is not None:
        request.node.add_marker(
            pytest.mark.xfail(strict=True, reason=f"{method} {path}: {reason}")
        )

    _drive(RECIPES, method, path, code, body_schema, expect_rows=True)


_EMPTY_OPERATIONS = [
    (method, path, code, schema)
    for method, path, code, schema in OPERATIONS
    if _recipe_for(EMPTY_STATE, method, path, code) is not None
]


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    _EMPTY_OPERATIONS,
    ids=[f"{m} {p} {c}" for m, p, c, _ in _EMPTY_OPERATIONS],
)
def test_the_wire_matches_the_declared_response_when_there_is_nothing_there(
    method, path, code, body_schema, request
):
    """The same claim, asked in the state where the nulls live."""
    reason = _mismatch_reason(method, path, "empty")
    if reason is not None:
        request.node.add_marker(
            pytest.mark.xfail(strict=True, reason=f"{method} {path}: {reason}")
        )

    _drive(EMPTY_STATE, method, path, code, body_schema, expect_rows=False)


def test_the_undeclared_key_check_is_not_blind():
    """The enumeration half, canaried the way the validation half is.

    A check that silently returned an empty list for every input would look
    exactly like a clean run. So: give it a real body and a schema that
    enumerates only one of its keys, and require it to name the rest.
    """
    schema = {"type": "object", "properties": {"kept": {"type": "string"}}}
    body = {"kept": "yes", "extra": 1, "another": None}
    assert sorted(_undeclared_keys(body, schema)) == ["another", "extra"]

    # A schema that declines to enumerate (a free-form map) reports nothing,
    # and a nested object is walked rather than skipped.
    assert _undeclared_keys(body, {"type": "object", "additionalProperties": {}}) == []
    nested = {
        "type": "object",
        "properties": {"inner": {"type": "object", "properties": {}}},
    }
    assert _undeclared_keys({"inner": {"surprise": 1}}, nested) == []


def test_the_gate_is_not_blind():
    """A canary: swap a declared schema for one the wire cannot satisfy.

    Everything above can be green for two reasons — the claims are honest, or
    the check never looks at the body. This tells them apart by validating a
    real response against ``{"type": "string"}``: every operation here answers
    an object or an array, so every one of them must fail. If any passes, the
    validation in ``_drive`` is not reaching the received body and this whole
    file proves nothing. With ``KNOWN_MISMATCHES`` empty this covers the
    entire declared surface.
    """
    honest = [
        (method, path, code)
        for method, path, code, _schema in OPERATIONS
        if (method, path) not in KNOWN_MISMATCHES and (method, path) not in UNDRIVABLE
    ]
    assert honest, "nothing left to canary"

    survivors = []
    for method, path, code in honest:
        try:
            _drive(RECIPES, method, path, code, {"type": "string"}, expect_rows=False)
        except AssertionError:
            continue
        survivors.append(f"{method} {path}")
    assert not survivors, (
        "these operations passed validation against {'type': 'string'} — the "
        "gate is not looking at the body it received:\n  " + "\n  ".join(survivors)
    )
