from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openedx_bulk_rerun_ext', '0003_coursererunjob_settings_applied_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='coursererunjob',
            name='target_course_key',
            field=models.CharField(max_length=255),
        ),
    ]
