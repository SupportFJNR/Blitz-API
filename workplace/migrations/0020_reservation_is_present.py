# Generated by Django 2.0.8 on 2018-11-16 14:09

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workplace', '0019_model_translation'),
    ]

    operations = [
        migrations.AddField(
            model_name='historicalreservation',
            name='is_present',
            field=models.BooleanField(default=False, verbose_name='Present'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='reservation',
            name='is_present',
            field=models.BooleanField(default=True, verbose_name='Present'),
            preserve_default=False,
        ),
    ]
