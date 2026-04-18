"""
apps/letterhead/migrations/0001_initial.py

Run:
    python manage.py migrate letterhead
"""
import uuid
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='LetterheadTemplate',
            fields=[
                ('id', models.UUIDField(
                    default=uuid.uuid4, editable=False, primary_key=True, serialize=False,
                )),
                ('name', models.CharField(max_length=120)),
                ('is_active', models.BooleanField(db_index=True, default=False)),
                ('template_key', models.CharField(
                    choices=[
                        ('classic',  'Classic'),
                        ('modern',   'Modern'),
                        ('bold',     'Bold'),
                        ('minimal',  'Minimal'),
                        ('split',    'Split'),
                        ('elegant',  'Elegant'),
                    ],
                    default='classic',
                    max_length=20,
                )),
                ('company_name', models.CharField(max_length=200)),
                ('tagline',      models.CharField(blank=True, max_length=200)),
                ('address',      models.TextField(blank=True)),
                ('contact_info', models.CharField(
                    blank=True, max_length=300,
                    help_text='Phone, email, website — one line',
                )),
                ('logo', models.ImageField(
                    blank=True, null=True,
                    upload_to='letterhead/logos/',
                    help_text='PNG / SVG / JPG.  Displayed at ~48 px height.',
                )),
                ('color_primary',   models.CharField(default='#1D9E75', max_length=7)),
                ('color_secondary', models.CharField(default='#2C2C2A', max_length=7)),
                ('font_header', models.CharField(
                    choices=[
                        ('Georgia',           'Georgia'),
                        ("'Times New Roman'", 'Times New Roman'),
                        ('Palatino',          'Palatino'),
                        ('Arial',             'Arial'),
                        ('Helvetica',         'Helvetica'),
                        ('Verdana',           'Verdana'),
                        ('Trebuchet MS',      'Trebuchet MS'),
                        ("'Courier New'",     'Courier New'),
                    ],
                    default='Georgia',
                    max_length=40,
                )),
                ('font_body', models.CharField(
                    choices=[
                        ('Georgia',           'Georgia'),
                        ("'Times New Roman'", 'Times New Roman'),
                        ('Palatino',          'Palatino'),
                        ('Arial',             'Arial'),
                        ('Helvetica',         'Helvetica'),
                        ('Verdana',           'Verdana'),
                        ('Trebuchet MS',      'Trebuchet MS'),
                        ("'Courier New'",     'Courier New'),
                    ],
                    default='Arial',
                    max_length=40,
                )),
                ('created_at',  models.DateTimeField(auto_now_add=True)),
                ('updated_at',  models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='letterhead_templates_created',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('last_edited_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='letterhead_templates_edited',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'verbose_name':        'Letterhead template',
                'verbose_name_plural': 'Letterhead templates',
                'ordering':            ['-is_active', '-updated_at'],
            },
        ),
    ]
