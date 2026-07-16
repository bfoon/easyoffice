"""
customforms 0002 — file/photo uploads + external email approvers.

The token field is added in three steps (nullable → per-row backfill →
unique) so existing SubmissionStep rows each get their own token.
"""
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.customforms.models


def backfill_tokens(apps_reg, schema_editor):
    SubmissionStep = apps_reg.get_model('customforms', 'SubmissionStep')
    for step in SubmissionStep.objects.filter(token__isnull=True).only('pk'):
        step.token = uuid.uuid4()
        step.save(update_fields=['token'])


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('customforms', '0001_initial'),
    ]

    operations = [
        # ── SubmissionStep: external approver support ────────────────────
        migrations.AlterField(
            model_name='submissionstep',
            name='assignee_type',
            field=models.CharField(choices=[
                ('user', 'Specific user'), ('role', 'Anyone with role'),
                ('position', 'Anyone with position'), ('unit', 'Anyone in unit'),
                ('submitter_choice', 'Submitter chooses at submission'),
                ('email', 'External person (email link)')], max_length=20),
        ),
        migrations.AddField(
            model_name='submissionstep',
            name='external_email',
            field=models.EmailField(blank=True, default='', max_length=254),
        ),
        migrations.AddField(
            model_name='submissionstep',
            name='external_name',
            field=models.CharField(blank=True, default='',
                                   help_text='Name typed by the external approver when acting.',
                                   max_length=120),
        ),
        migrations.AddField(
            model_name='submissionstep',
            name='token',
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(backfill_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='submissionstep',
            name='token',
            field=models.UUIDField(default=uuid.uuid4, editable=False,
                                   help_text='Secret token for the external approval link.',
                                   unique=True),
        ),

        # ── FormUpload ────────────────────────────────────────────────────
        migrations.CreateModel(
            name='FormUpload',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False,
                                        primary_key=True, serialize=False)),
                ('file', models.FileField(upload_to=apps.customforms.models._upload_path)),
                ('original_name', models.CharField(max_length=255)),
                ('size', models.PositiveBigIntegerField(default=0)),
                ('content_type', models.CharField(blank=True, default='', max_length=120)),
                ('is_image', models.BooleanField(default=False)),
                ('field_id', models.CharField(blank=True, default='', max_length=40)),
                ('file_app_ref', models.CharField(blank=True, default='', max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('submission', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='uploads', to='customforms.formsubmission')),
                ('uploaded_by', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='form_uploads', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]
