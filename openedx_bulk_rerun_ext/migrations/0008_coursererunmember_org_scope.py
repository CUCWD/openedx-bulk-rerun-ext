from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openedx_bulk_rerun_ext', '0007_coursererunsettings_gating_min_fields'),
    ]

    operations = [
        # Add org field (blank/default so existing rows stay valid).
        migrations.AddField(
            model_name='CourseRerunTeamMember',
            name='org',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Organization short_name this member belongs to. Empty = apply to all orgs (legacy).',
                max_length=64,
            ),
        ),
        # Widen uniqueness constraint to include org so the same email can
        # appear in multiple orgs within the same batch.
        migrations.AlterUniqueTogether(
            name='CourseRerunTeamMember',
            unique_together={('batch', 'org', 'email')},
        ),
        # Update ordering to sort by org first, then email.
        migrations.AlterModelOptions(
            name='CourseRerunTeamMember',
            options={'ordering': ['org', 'email']},
        ),
    ]
