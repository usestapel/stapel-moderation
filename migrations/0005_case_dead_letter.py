"""The dead-letter park: three columns and one new state value.

``dlq_at`` / ``last_error_class`` / ``last_error`` are what a case carries
once the screening seam has given up on it. The state value ``dlq`` itself
needs no schema change — ``Case.state`` is a plain ``CharField`` with
``choices``, so the vocabulary lives in Python and this migration only
records that it moved.

Additive and nullable/blank throughout. Every existing case reads as "never
dead-lettered", which is true of all of them: before 0.7.0 a screening
failure was written down as a ``needs_review`` verdict in the human queue,
which is the behaviour this release ends.

**No data migration, deliberately.** Cases already carrying a
``policy_default / needs_review / screening_unavailable`` verdict are left
exactly as they are: rewriting a recorded verdict — even a wrong one — is
not something a schema migration gets to do to an append-only audit trail
that feeds DSA Art. 17 statements of reasons. Move them with the
``moderation_rescreen`` management command, which re-screens them through
the ladder and lets the new states be reached the way every other case
reaches them.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('moderation', '0004_case_rescreen_recovery'),
    ]

    operations = [
        migrations.AddField(
            model_name='case',
            name='dlq_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='case',
            name='last_error_class',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='case',
            name='last_error',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
        migrations.AlterField(
            model_name='case',
            name='state',
            field=models.CharField(
                choices=[
                    ('open', 'Open'),
                    ('screening', 'Screening'),
                    ('queued', 'Queued'),
                    ('claimed', 'Claimed'),
                    ('dlq', 'Dead-lettered'),
                    ('resolved', 'Resolved'),
                ],
                db_index=True,
                default='open',
                max_length=16,
            ),
        ),
    ]
