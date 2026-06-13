"""
Initial migration for the poi app.

NOTE: dependencies reference your real apps by label:
  - "core"  for the User model (POIProfile.user / created_by / disabled_by)
  - "files" for FileFolder   (POIProfile.workspace_folder)

‼️ If your User app label is not "core" or your files app label is not "files",
update the dependencies and the swappable/FK targets below, then run:
    python manage.py makemigrations poi   # to let Django reconcile, or keep this.
"""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("files", "__first__"),
    ]

    operations = [
        migrations.CreateModel(
            name="POIProfile",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(
                    choices=[("invited", "Invited"), ("active", "Active"),
                             ("disabled", "Disabled"), ("expired", "Expired")],
                    default="invited", max_length=20)),
                ("trusted_until", models.DateTimeField(blank=True, null=True)),
                ("device_fingerprint", models.CharField(blank=True, max_length=255)),
                ("disabled_at", models.DateTimeField(blank=True, null=True)),
                ("disabled_reason", models.CharField(blank=True, max_length=255)),
                ("notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="poi_profile", to=settings.AUTH_USER_MODEL)),
                ("created_by", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="created_pois", to=settings.AUTH_USER_MODEL)),
                ("disabled_by", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="disabled_pois", to=settings.AUTH_USER_MODEL)),
                ("workspace_folder", models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+", to="files.filefolder")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="poiprofile",
            index=models.Index(fields=["status", "trusted_until"],
                               name="poi_poiprof_status_d1e425_idx"),
        ),
        migrations.CreateModel(
            name="POIInvite",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("token", models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("profile", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="invites", to="poi.poiprofile")),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
