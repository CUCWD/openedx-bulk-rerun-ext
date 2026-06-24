from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openedx_bulk_rerun_ext', '0004_remove_coursererunjob_target_course_key_unique'),
    ]

    operations = [
        migrations.AddField(
            model_name='bulkrerunbatch',
            name='config_json',
            field=models.JSONField(
                blank=True,
                default=None,
                null=True,
                help_text=(
                    'Snapshot of the wizard cfg object (rows, prog, fromMode, etc.) '
                    'stored at submission time so the progress UI can reconstruct '
                    'display context on any device or after a page refresh.'
                ),
            ),
        ),
    ]
