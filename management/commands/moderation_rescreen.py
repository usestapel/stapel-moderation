"""``manage.py moderation_rescreen`` — the one-off that empties a park.

The operator-side half of the dead-letter state. A release that repairs a
screening seam — a proxy that is reachable again, a content function that
resolves the keys it is asked for, an API key that finally landed — leaves
behind every case the seam broke on, and the beat sweep will get to them
eventually, on an exponential backoff, three at a time per case. This is the
button for "it is fixed, do it now".

It also carries the migration this release deliberately does NOT do in SQL.
Cases already holding a ``policy_default / needs_review /
screening_unavailable`` verdict from before 0.7.0 are not rewritten by a
schema migration — an append-only audit trail that feeds DSA Art. 17
statements of reasons is not something a migration gets to edit. Run this
instead: each case goes back through the ladder and reaches the new states
the way every other case reaches them, with the audit rows to show it.

Every mode is a re-screen or a close. **Nothing here approves anything**,
which is the same line ``tasks.rescreen_stuck_cases`` holds and for the same
reason: legacy's stuck-case job swept ``needs_review`` into auto-approval on
the pass that retried, and published unmoderated listings for years.

**It dispatches; it does not screen.** Each case goes onto the worker as a
``moderation.screen`` comm task — the same path the ladder and the beat sweep
use — and the command reports how many were handed over, not how many were
decided. The reason is where this runs: an operator types it into
``docker compose run --rm web manage.py …``, a one-off container that no
Prometheus scrapes and that exits the moment the command returns. Screening
in there means the screening metrics, the failure counters and the DLQ alert
that watches them are all written to a process nobody is looking at: on a
client stand a manual rescreen of 122 cases produced no
``moderation_screen_failures_total`` movement at all, and the operator read
that silence as success. Dispatched, the same 122 screenings run in the
worker, where every failure lands on the same counters as the automatic ones.

``--sync`` is the debug flag that screens in this process instead, and it
carries that cost: **its outcomes are unobserved**. Use it to watch one case
go through a repaired seam, not to empty a park.

Examples::

    manage.py moderation_rescreen --state dlq                 # the repair
    manage.py moderation_rescreen --state queued --dry-run    # what would move
    manage.py moderation_rescreen --state queued --limit 200
    manage.py moderation_rescreen --state dlq --error-class ContentUnavailable
    manage.py moderation_rescreen --state dlq --limit 1 --sync # debug, unobserved
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Re-screen or close cases the automatic screener could not decide."

    def add_arguments(self, parser):
        parser.add_argument(
            "--state",
            default="dlq",
            help=(
                "Which population to move: dlq (the default — cases the "
                "screening seam broke on) or queued (cases parked in the "
                "human queue, including pre-0.7.0 screening-failure rows)."
            ),
        )
        parser.add_argument(
            "--target-type",
            default="",
            help="Restrict to one target type (listing, review, …).",
        )
        parser.add_argument(
            "--error-class",
            default="",
            help=(
                "Restrict to one failure class — ContentUnavailable, "
                "ScreeningUnavailable, TargetNotFound. Repair one seam at a "
                "time rather than re-billing every case in the park."
            ),
        )
        parser.add_argument(
            "--origin",
            default="",
            help="Restrict to one origin (submission, report, draft, …).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Stop after this many cases. 0 (the default) means all of them.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would move and change nothing.",
        )
        parser.add_argument(
            "--sync",
            action="store_true",
            help=(
                "Debug only: screen in THIS process instead of dispatching to "
                "the worker. The screenings and their failures are then "
                "unobserved — no scrape reaches a one-off container — so the "
                "DLQ alert cannot see what this run breaks on."
            ),
        )

    def handle(self, *args, **options):
        from stapel_core.comm import comm_setting
        from stapel_core.comm.tasks import execute

        from stapel_moderation import services
        from stapel_moderation.models import Case, CaseState

        state = str(options["state"]).strip()
        valid = {s for s in CaseState.values}
        if state not in valid:
            raise CommandError(f"--state must be one of {sorted(valid)}, not {state!r}")

        rows = Case.objects.filter(state=state)
        if options["target_type"]:
            rows = rows.filter(target_type=options["target_type"])
        if options["error_class"]:
            rows = rows.filter(last_error_class=options["error_class"])
        if options["origin"]:
            rows = rows.filter(origin=options["origin"])
        rows = rows.order_by("created_at")
        if options["limit"]:
            rows = rows[: options["limit"]]

        cases = list(rows)
        # Split before doing anything, because the two answers are different
        # actions and an operator is entitled to see the split first: a case
        # whose subject cannot be addressed is CLOSED, never re-screened —
        # re-screening it is the defect this release removed.
        gone = [c for c in cases if not services.target_is_addressable(c)]
        screenable = [c for c in cases if services.target_is_addressable(c)]

        self.stdout.write(
            f"{len(cases)} case(s) in state {state!r}: "
            f"{len(screenable)} to re-screen, {len(gone)} to close as subject_gone"
        )
        if options["dry_run"]:
            for case in cases:
                verb = "close" if case in gone else "re-screen"
                self.stdout.write(
                    f"  {verb:9} {case.id} {case.target_type}:{case.target_key} "
                    f"origin={case.origin} error={case.last_error_class or '-'}"
                )
            self.stdout.write(self.style.WARNING("dry run — nothing was changed"))
            return

        closed = 0
        for case in gone:
            try:
                services.close_subject_gone(case)
                closed += 1
            except services.ModerationError as exc:
                self.stderr.write(f"  could not close {case.id}: {exc}")

        sync = bool(options["sync"])
        dispatched = 0
        queued = 0
        for case in screenable:
            try:
                task_id = services.rescan_case(case)
            except services.ModerationError as exc:
                self.stderr.write(f"  could not re-screen {case.id}: {exc}")
                continue
            if task_id is None:
                # Automation is off for this target type or for this
                # deployment, so `rescan_case` parked the case in the human
                # queue rather than on the ladder. Nothing was handed to a
                # worker, so it must not be counted as if it had been.
                queued += 1
                continue
            dispatched += 1
            if sync:
                # Claim and run the task the dispatch just created, here.
                # A no-op when the host is configured to run tasks inline
                # (`start()` already executed it and the record is no longer
                # PENDING) — which is exactly the case this flag exists to
                # make explicit rather than accidental.
                execute(task_id)

        verb = "screened here" if sync else "dispatched"
        parts = [f"{verb} {dispatched}", f"closed {closed} as subject_gone"]
        if queued:
            parts.append(f"{queued} sent to the human queue (screening off)")
        self.stdout.write(self.style.SUCCESS(", ".join(parts)))

        if sync:
            self.stdout.write(
                self.style.WARNING(
                    "--sync screened in this process: no scrape reaches it, so "
                    "these failures are invisible to the DLQ alert."
                )
            )
        elif dispatched and _runs_tasks_in_process(comm_setting):
            # Do not claim a handover that the configuration cancels. With
            # TASK_DISPATCH/TASK_EXECUTOR both inline, `start()` runs the
            # screening in THIS container and the count above says
            # "dispatched" about work that already happened here, unobserved
            # — the exact silence this command was changed to remove.
            self.stdout.write(
                self.style.WARNING(
                    "STAPEL_COMM runs tasks inline in this process, so these "
                    f"{dispatched} screening(s) ran here and are as unobserved "
                    "as --sync. Set TASK_DISPATCH to 'action' (or 'bus') and "
                    "TASK_EXECUTOR to a worker to have them scraped."
                )
            )


def _runs_tasks_in_process(comm_setting) -> bool:
    """Whether a dispatched task is executed by the process that starts it.

    Two configurations do that. ``TASK_DISPATCH = "inline"`` says so outright.
    The quieter one is a monolith: the announcement rides the in-process
    action transport, so this container's own ``task.requested`` subscriber
    receives it, and an ``inline`` executor then runs the handler right here.
    Anything that puts the announcement on a broker — ``TASK_DISPATCH="bus"``,
    or an ``ACTION_TRANSPORT`` that is not in-process — hands it to a worker,
    whose executor setting is the worker's business and not this container's.
    """
    dispatch = comm_setting("TASK_DISPATCH", "action")
    if dispatch == "inline":
        return True
    if dispatch != "action":
        return False
    if comm_setting("ACTION_TRANSPORT", "inprocess") not in ("inprocess", "memory"):
        return False
    return comm_setting("TASK_EXECUTOR", "inline") == "inline"
