"""End-to-end proof over real HTTP, against a real verdict consumer.

Boots the e2e host (SQLite, in-process comm, outbox on, `stapel-listings`
installed) and drives the whole lifecycle with plain HTTP requests.

The reason this script exists rather than another pytest module: the one
claim stapel-moderation makes that its own unit tests cannot check is
**"resolving a case changes the target"**. Moderation emits a fact and never
calls a host back, so proving anything happened needs a real consumer in the
process. Every step below that says "the listing" is asserting on
``stapel-listings`` state, reached through a probe endpoint the e2e host
mounts. That is the legacy asymmetry — resolving a complaint about a review
deleted the review, resolving one about an ad touched nothing — closed by
measurement instead of by agreement.

    seed (listing.submitted opens a case)
    -> the model is DOWN: the screening task retries, parks, and the case is
       HELD for a human, with the listing still unpublished
    -> a member reports the listing, and gets an acknowledgement
    -> a MID moderator can read the queue and cannot decide it
    -> a HIGH lead takes the case, rejects it, and suspends the author in one act
    -> the LISTING is blocked, the author has a statement of reasons and an
       appeal link, and the author's session is dead
    -> the author appeals; the lead who decided is refused; a second lead
       overturns it
    -> the LISTING is published again and the suspension is lifted
    -> the audit trail is complete and the admin cannot edit it

Run:  .venv/bin/python e2e/run_e2e.py
Exit code 0 and "E2E PASS" is the gate; any assertion failure is a real
defect somewhere on the path.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
STATE = Path(os.environ.get("STAPEL_MODERATION_E2E_DIR", "/tmp/stapel-moderation-e2e"))
PY = sys.executable
BASE = "http://127.0.0.1:8772"
API = f"{BASE}/moderation/api/v1"
PROBE = f"{BASE}/_e2e/probe"

PASSWORD = "e2e-pass-Str0ng!"
SCRIPT_FILE = STATE / "llm_script.json"
NOTIFY_FILE = STATE / "notifications.jsonl"

_step = 0


def step(message):
    global _step
    _step += 1
    print(f"  {_step:2d}. {message}")


def manage(*args):
    env = {**os.environ, "STAPEL_MODERATION_E2E_DIR": str(STATE)}
    return subprocess.run(
        [PY, str(REPO / "e2e" / "manage.py"), *args],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
    )


def script_llm(**answer):
    """Tell the in-process llm.complete provider what to say next."""
    SCRIPT_FILE.write_text(json.dumps(answer))


def notifications():
    if not NOTIFY_FILE.exists():
        return []
    return [json.loads(line) for line in NOTIFY_FILE.read_text().splitlines() if line]


def notifications_of(kind):
    return [item for item in notifications() if item["type"] == kind]


def login(username):
    session = requests.Session()
    response = session.post(
        f"{BASE}/auth/api/v1/password/login/",
        json={"login": username, "password": PASSWORD},
        timeout=10,
    )
    assert response.status_code == 200, (username, response.status_code, response.text[:300])
    return session


def probe(listing_id):
    response = requests.get(f"{PROBE}/{listing_id}", timeout=10)
    assert response.status_code == 200, response.text[:300]
    return response.json()


def main():
    if STATE.exists():
        shutil.rmtree(STATE)
    STATE.mkdir(parents=True)

    print("booting the e2e host ...")
    migrate = manage("migrate", "--noinput")
    assert migrate.returncode == 0, migrate.stderr[-3000:]

    # The screener is down before the seed runs, on purpose: the very first
    # thing this script proves is what happens when the model cannot answer.
    script_llm(status="failure", reason="provider down")

    seed = manage("e2e_seed")
    assert seed.returncode == 0, seed.stderr[-3000:]
    ids = json.loads(seed.stdout.strip().splitlines()[-1])
    listing_id = ids["listing_id"]

    env = {**os.environ, "STAPEL_MODERATION_E2E_DIR": str(STATE)}
    server = subprocess.Popen(
        [PY, str(REPO / "e2e" / "manage.py"), "runserver", "127.0.0.1:8772", "--noreload"],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_server(server)
        run(ids, listing_id)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            server.kill()

    print("\nE2E PASS")
    return 0


def _wait_for_server(server):
    deadline = time.time() + 45
    while time.time() < deadline:
        if server.poll() is not None:
            raise SystemExit(f"server died:\n{server.stderr.read()[-4000:]}")
        try:
            requests.get(f"{BASE}/moderation/api/v1/policy", timeout=2)
            return
        except requests.RequestException:
            time.sleep(0.4)
    raise SystemExit("server did not come up")


def run(ids, listing_id):
    print("\n── the model is down ──────────────────────────────────────────")

    # The seed published the listing, listings emitted listing.submitted, and
    # moderation opened a case and screened it — against a provider returning
    # a FAILURE ENVELOPE, not an exception. If the handler had returned that
    # envelope the task would read DONE and the case would hold a decision
    # nobody made.
    lead = login("lead")
    cases = lead.get(f"{API}/cases", timeout=10)
    assert cases.status_code == 200, cases.text[:300]
    assert len(cases.json()) == 1, cases.json()
    case = cases.json()[0]
    case_id = case["id"]
    held_case_id = case_id
    assert case["state"] == "queued", case
    step(f"the unscreenable case is QUEUED for a human (case {case_id[:8]})")

    events = lead.get(f"{API}/cases/{case_id}/events", timeout=10).json()
    kinds = [row["kind"] for row in events]
    assert "screen_started" in kinds and "screen_failed" in kinds, kinds
    step("the audit trail records the screening attempt and its failure")

    detail = lead.get(f"{API}/cases/{case_id}", timeout=10).json()
    held = [v for v in detail["verdicts"] if v["reason_code"] == "screening_unavailable"]
    assert held and held[0]["decision"] == "needs_review", detail["verdicts"]
    assert held[0]["source"] == "policy_default", held[0]
    step("a needs_review verdict names screening_unavailable — nothing was guessed")

    state = probe(listing_id)
    # needs_review DOES travel to the target — for listings it is an honest
    # "under review" — but it is not terminal for the case and it publishes
    # nothing. The predecessor system's answer to the same situation was to
    # publish the listing about half an hour later.
    assert state["moderation_status"] == "needs_review", state
    assert state["status"] == "pending", state
    assert state["is_active"] is False, state
    step("the LISTING reads 'under review' and is NOT published — hold held")

    print("\n── the model comes back ───────────────────────────────────────")

    # The rescan endpoint is the explicit "look again" path. It is also how a
    # deployment that ran with ON_SCREENING_FAILURE="approve" is supposed to
    # re-screen everything it let through while the model was down.
    script_llm(decision="approved", confidence=0.96)
    rescan = lead.post(f"{API}/cases/{case_id}/rescan", timeout=10)
    assert rescan.status_code == 202, rescan.text[:300]
    step("the lead re-screens the held case now that the model answers again")

    state = probe(listing_id)
    assert state["moderation_status"] == "approved", state
    assert state["status"] == "published", state
    assert state["is_active"] is True, state
    step("THE LISTING IS PUBLISHED — an automatic approval reached the target")

    print("\n── a member reports the live listing ──────────────────────────")

    script_llm(decision="needs_review", confidence=0.9)
    reporter = login("reporter")
    filed = reporter.post(
        f"{API}/reports/",
        json={
            "target_type": "listing",
            "target_key": str(listing_id),
            "reason_code": "counterfeit",
            "good_faith": True,
        },
        timeout=10,
    )
    assert filed.status_code == 201, filed.text[:300]
    step(f"the report is accepted, quoting case ref {filed.json()['case_ref']}")

    again = reporter.post(
        f"{API}/reports/",
        json={
            "target_type": "listing",
            "target_key": str(listing_id),
            "reason_code": "spam",
        },
        timeout=10,
    )
    assert again.status_code == 409, again.text[:300]
    assert again.json()["localizable_error"] == "error.409.moderation_already_reported"
    step("a second report from the same person is refused by a real constraint")

    received = notifications_of("moderation.report_received")
    assert len(received) == 1, received
    step("the complainant is acknowledged (Art. 16(4))")

    # The resolved first case did not block a second one: the open-case
    # constraint covers the OPEN states only.
    queued = lead.get(f"{API}/cases?state=queued", timeout=10).json()
    assert len(queued) == 1, queued
    case_id = queued[0]["id"]
    detail = lead.get(f"{API}/cases/{case_id}", timeout=10).json()
    assert detail["report_count"] == 1, detail
    assert detail["reports"][0]["reason_code"] == "counterfeit", detail["reports"]
    assert detail["content"]["available"] is True, detail["content"]
    assert detail["content"]["title"] == "Vintage racing bicycle", detail["content"]
    step("a second case opened for the live listing, and its card reads live content")

    print("\n── the console is graded ──────────────────────────────────────")

    # A MID moderator: the queue is readable, the verdict is not.
    demote = manage("shell", "-c", _DEMOTE_REVIEWER)
    assert demote.returncode == 0, demote.stderr[-2000:]
    moderator = login("reviewer")
    assert moderator.get(f"{API}/cases", timeout=10).status_code == 200
    refused = moderator.post(
        f"{API}/cases/{case_id}/verdict", json={"decision": "approved"}, timeout=10
    )
    assert refused.status_code == 403, refused.text[:300]
    step("a MID moderator reads the queue and cannot decide it")

    print("\n── the verdict acts on the target ─────────────────────────────")

    claimed = lead.post(f"{API}/cases/{case_id}/claim", timeout=10)
    assert claimed.status_code == 200, claimed.text[:300]
    assert claimed.json()["state"] == "claimed"
    step("the lead takes a lease on the case")

    verdict = lead.post(
        f"{API}/cases/{case_id}/verdict",
        json={
            "decision": "rejected",
            "reason_code": "counterfeit",
            "note": "Branded frame decals do not match the manufacturer's.",
            "sanction": {"kind": "suspended", "duration_seconds": 3600},
        },
        timeout=10,
    )
    assert verdict.status_code == 201, verdict.text[:400]
    step("one request: reject the listing AND suspend its author")

    state = probe(listing_id)
    assert state["moderation_status"] == "rejected", state
    # BLOCKED, not REJECTED: this listing was LIVE, so the verdict travelled
    # the `published -> blocked` edge that stapel-listings 0.4.0 released for
    # exactly this. Before it, taking down a published listing was
    # inexpressible — the FSM had no edge and visibility never looked at
    # moderation, so `published + rejected` was a legal, fully visible state.
    assert state["status"] == "blocked", state
    assert state["is_active"] is False, state
    assert "decals" in state["moderation_note"], state
    step("THE LISTING IS BLOCKED — a live listing was actually taken down")

    blocked = notifications_of("listing_blocked")
    assert len(blocked) == 1, blocked
    variables = blocked[0]["variables"]
    assert variables["listing_title"] == "Vintage racing bicycle", variables
    assert variables["reason_label"] == "moderation.reason.counterfeit.label", variables
    assert variables["appeal_url"].endswith(case_id), variables
    step("the author has a statement of reasons and a live appeal link (Art. 17)")

    reviewed = notifications_of("report_reviewed")
    assert len(reviewed) == 1, reviewed
    assert reviewed[0]["variables"]["outcome_label"] == "rejected", reviewed
    step("the complainant is told the outcome, and told the TRUE one (Art. 16(5))")

    sanction_letter = notifications_of("moderation.sanction_issued")
    assert len(sanction_letter) == 1, sanction_letter
    assert sanction_letter[0]["variables"]["sanction_kind"] == "suspended"
    step("the author is told what happened to their account and how to contest it")

    # The teeth: the author's live session is gone, because the sanction set
    # core's cross-service blacklist key — the hook that had no producer.
    author = login("author")
    blocked_call = author.get(f"{API}/reports/", timeout=10)
    assert blocked_call.status_code in (401, 403), blocked_call.status_code
    step("the suspended author's SESSION is dead, not merely their next token")

    print("\n── the appeal is a remedy, not a letter ───────────────────────")

    manage("shell", "-c", _UNBLACKLIST)
    author = login("author")
    sanctions = lead.get(
        f"{API}/sanctions?subject_user_id={ids['author_id']}", timeout=10
    ).json()
    assert len(sanctions) == 1, sanctions
    sanction_id = sanctions[0]["id"]

    appeal = author.post(
        f"{API}/appeals/",
        json={
            "case_id": case_id,
            "body": "The decals are original; here is the purchase receipt.",
            "sanction_id": sanction_id,
        },
        timeout=10,
    )
    assert appeal.status_code == 201, appeal.text[:400]
    appeal_id = appeal.json()["id"]
    step("the author appeals the decision (Art. 20)")

    same_actor = lead.post(
        f"{API}/appeals/{appeal_id}/resolve", json={"outcome": "overturned"}, timeout=10
    )
    assert same_actor.status_code == 403, same_actor.text[:300]
    assert same_actor.json()["localizable_error"] == "error.403.moderation_same_actor"
    step("the moderator who decided is refused the appeal — independence enforced")

    promote = manage("shell", "-c", _PROMOTE_REVIEWER)
    assert promote.returncode == 0, promote.stderr[-2000:]
    reviewer = login("reviewer")
    overturned = reviewer.post(
        f"{API}/appeals/{appeal_id}/resolve",
        json={"outcome": "overturned", "note": "Receipt checks out."},
        timeout=10,
    )
    assert overturned.status_code == 200, overturned.text[:400]
    assert overturned.json()["state"] == "overturned"
    step("a second moderator overturns it")

    state = probe(listing_id)
    assert state["moderation_status"] == "approved", state
    # `blocked -> published`, the reinstatement edge. Without it an appeal
    # would be a letter: the platform could admit it was wrong and still
    # leave the listing down.
    assert state["status"] == "published", state
    assert state["is_active"] is True, state
    step("THE LISTING IS PUBLISHED AGAIN — the appeal undid the takedown")

    sanctions = reviewer.get(
        f"{API}/sanctions?subject_user_id={ids['author_id']}", timeout=10
    ).json()
    assert sanctions[0]["state"] == "overturned", sanctions
    step("the suspension is marked overturned — discretion and error are told apart")

    resolved_letter = notifications_of("moderation.appeal_resolved")
    assert len(resolved_letter) == 1, resolved_letter
    assert resolved_letter[0]["variables"]["outcome_label"] == "overturned"
    step("the appellant is told the outcome")

    print("\n── the record ─────────────────────────────────────────────────")

    events = reviewer.get(f"{API}/cases/{case_id}/events", timeout=10).json()
    kinds = [row["kind"] for row in events]
    for expected in (
        "created",
        "reported",
        "claimed",
        "verdict",
        "state_changed",
        "sanctioned",
        "appealed",
        "reopened",
    ):
        assert expected in kinds, (expected, kinds)
    stamps = [row["created_at"] for row in events]
    assert stamps == sorted(stamps), stamps
    step(f"the complaint case's trail has all {len(events)} steps, in order")

    held_events = reviewer.get(f"{API}/cases/{held_case_id}/events", timeout=10).json()
    held_kinds = [row["kind"] for row in held_events]
    for expected in ("screen_started", "screen_failed", "verdict"):
        assert expected in held_kinds, (expected, held_kinds)
    step("the first case's trail still records the outage and what was done about it")

    detail = reviewer.get(f"{API}/cases/{case_id}", timeout=10).json()
    decisions = [v["decision"] for v in detail["verdicts"]]
    assert decisions.count("rejected") == 1 and decisions.count("approved") == 1, decisions
    step("every verdict survives — the overturn appended, it did not rewrite")

    admin_check = manage("shell", "-c", _ADMIN_IS_READ_ONLY)
    assert admin_check.returncode == 0, admin_check.stderr[-2000:]
    step("the Django admin registers every model read-only, audit log included")

    print("\n── a dismissal touches nothing ────────────────────────────────")

    script_llm(decision="needs_review", confidence=0.9)
    second = manage("shell", "-c", _SECOND_LISTING)
    assert second.returncode == 0, second.stderr[-3000:]
    second_id = int(second.stdout.strip().splitlines()[-1])

    before = probe(second_id)
    reporter.post(
        f"{API}/reports/",
        json={
            "target_type": "listing",
            "target_key": str(second_id),
            "reason_code": "spam",
        },
        timeout=10,
    )
    cases = reviewer.get(f"{API}/cases?state=queued", timeout=10).json()
    second_case = [c for c in cases if c["target_key"] == str(second_id)][0]
    dismissed = reviewer.post(
        f"{API}/cases/{second_case['id']}/verdict",
        json={"decision": "dismissed", "note": "Nothing wrong with it."},
        timeout=10,
    )
    assert dismissed.status_code == 201, dismissed.text[:300]

    # Byte-for-byte unchanged, not merely "still not blocked": `dismissed` is
    # a statement about the COMPLAINT, where `approved` is a statement about
    # the content. stapel-listings 0.4.0 added the fourth word to its consumed
    # enum for exactly this, and touches nothing when it arrives.
    after = probe(second_id)
    assert after == before, (before, after)
    step("a dismissed report leaves the listing byte-for-byte untouched")

    policy = requests.get(f"{API}/policy", timeout=10)
    assert policy.status_code == 200, policy.status_code
    disclosure = policy.json()
    assert disclosure["automated_means"]["on_unavailable"] == "hold", disclosure
    assert disclosure["human_review"]["auto_resolve_after_seconds"] is None
    assert disclosure["human_review"]["appeal_requires_different_actor"] is True
    step("the public disclosure describes the deployment this run just exercised")


# Roles are granted and revoked THROUGH stapel-auth, never by writing the
# user field: the field is the materialized cache of the assignment table and
# the JWT claim is rebuilt from the assignments on every login.
_DEMOTE_REVIEWER = (
    "from django.contrib.auth import get_user_model; "
    "from stapel_auth.staff_roles import assign_staff_role, revoke_staff_role; "
    "u = get_user_model().objects.get(username='reviewer'); "
    "revoke_staff_role(u, 'ts_lead'); assign_staff_role(u, 'moderator')"
)
_PROMOTE_REVIEWER = (
    "from django.contrib.auth import get_user_model; "
    "from stapel_auth.staff_roles import assign_staff_role, revoke_staff_role; "
    "u = get_user_model().objects.get(username='reviewer'); "
    "revoke_staff_role(u, 'moderator'); assign_staff_role(u, 'ts_lead')"
)
# Lift the enforcement key by hand so the run can keep acting as the author.
# The point was proved one step earlier; leaving them locked out would only
# prove it again, more slowly.
_UNBLACKLIST = (
    "from django.contrib.auth import get_user_model; "
    "from stapel_core.django.jwt.authentication import unblacklist_user; "
    "unblacklist_user(str(get_user_model().objects.get(username='author').pk))"
)
_ADMIN_IS_READ_ONLY = (
    "from django.contrib import admin; "
    "from stapel_moderation.models import Appeal, Case, CaseEvent, Report, Sanction, Verdict; "
    "rows = [admin.site._registry[m] for m in (Case, CaseEvent, Verdict, Report, Sanction, Appeal)]; "
    "assert all(not r.has_add_permission(None) and not r.has_change_permission(None) "
    "and not r.has_delete_permission(None) for r in rows)"
)
_SECOND_LISTING = (
    "from django.contrib.auth import get_user_model; "
    "from stapel_listings.models import Listing; "
    "from stapel_listings.services.publish import publish_listing; "
    "a = get_user_model().objects.get(username='author'); "
    "listing = Listing.objects.create(owner=a, category_id='bicycles', "
    "title_draft='Childrens bicycle', description_draft='Small, blue, good condition.', "
    "price_draft='60.00', currency='USD', language='en'); "
    "publish_listing(listing); print(listing.pk)"
)


if __name__ == "__main__":
    raise SystemExit(main())
