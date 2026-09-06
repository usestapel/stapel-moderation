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

Examples::

    manage.py moderation_rescreen --state dlq                 # the repair
    manage.py moderation_rescreen --state queued --dry-run    # what would move
    manage.py moderation_rescreen --state queued --limit 200
    manage.py moderation_rescreen --state dlq --error-class ContentUnavailable
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

    def handle(self, *args, **options):
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

        started = 0
        for case in screenable:
            try:
                services.rescan_case(case)
                started += 1
            except services.ModerationError as exc:
                self.stderr.write(f"  could not re-screen {case.id}: {exc}")

        self.stdout.write(
            self.style.SUCCESS(
                f"re-screened {started}, closed {closed} as subject_gone"
            )
        )
