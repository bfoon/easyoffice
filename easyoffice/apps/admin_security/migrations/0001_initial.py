from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AdminAccessSetting',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False)),
                ('is_admin_enabled', models.BooleanField(default=True)),
                ('require_otp_for_admin', models.BooleanField(default=False)),
                ('otp_validity_minutes', models.PositiveSmallIntegerField(default=10)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('last_changed_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=models.deletion.SET_NULL,
                    related_name='+',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'verbose_name': 'Admin access setting',
                'verbose_name_plural': 'Admin access settings',
            },
        ),
        migrations.CreateModel(
            name='AdminEmailOTP',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False)),
                ('code_hash', models.CharField(max_length=64)),
                ('sent_to_email', models.EmailField(max_length=254)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('consumed_at', models.DateTimeField(blank=True, null=True)),
                ('expires_at', models.DateTimeField()),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('user_agent', models.CharField(blank=True, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(
                    on_delete=models.deletion.CASCADE,
                    related_name='admin_email_otps',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='adminemailotp',
            index=models.Index(fields=['user', 'consumed_at'], name='admin_secur_user_id_consumed_idx'),
        ),
    ]
