from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openedx_bulk_rerun_ext', '0006_remove_coursererunjob_gating_template_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='coursererunsettings',
            name='gating_min_score',
            field=models.CharField(blank=True, default='80', max_length=3),
        ),
        migrations.AddField(
            model_name='coursererunsettings',
            name='gating_min_completion',
            field=models.CharField(blank=True, default='100', max_length=3),
        ),
    ]
