from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openedx_bulk_rerun_ext', '0005_bulkrerunbatch_config_json'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='coursererunsettings',
            name='gating_template_id',
        ),
        migrations.AlterField(
            model_name='coursererunsettings',
            name='gating_mode',
            field=models.CharField(
                choices=[('disabled', 'Disabled'), ('copy', 'Copy from source'), ('custom', 'Custom (not implemented)')],
                default='disabled',
                max_length=16,
            ),
        ),
    ]
