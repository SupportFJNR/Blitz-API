# Generated by Django 2.2.12 on 2020-05-25 23:50

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('store', '0033_auto_20200525_1929'),
    ]

    operations = [
        migrations.AddField(
            model_name='historicalmembershipcoupon',
            name='limit_date',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Limit date'),
        ),
        migrations.AddField(
            model_name='membershipcoupon',
            name='limit_date',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Limit date'),
        ),
    ]
