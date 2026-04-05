import os
import uuid
from django.db import models
from apps.core.models import User


class FileFolder(models.Model):
    class Visibility(models.TextChoices):
        PRIVATE = 'private', 'Private'
        UNIT = 'unit', 'Unit'
        DEPARTMENT = 'department', 'Department'
        OFFICE = 'office', 'Office-wide'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='owned_folders')
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='subfolders')
    visibility = models.CharField(max_length=20, choices=Visibility.choices, default=Visibility.PRIVATE)
    unit = models.ForeignKey('organization.Unit', on_delete=models.SET_NULL, null=True, blank=True)
    department = models.ForeignKey('organization.Department', on_delete=models.SET_NULL, null=True, blank=True)
    color = models.CharField(max_length=7, default='#f59e0b')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def file_count(self):
        return self.files.filter(is_latest=True).count()

    def ancestors(self):
        """Return list of ancestor folders for breadcrumb."""
        result = []
        current = self.parent
        while current:
            result.insert(0, current)
            current = current.parent
        return result


# ---------------------------------------------------------------------------
# File type helpers (no custom template tags needed)
# ---------------------------------------------------------------------------

_EXT_MAP = {
    # Documents
    'pdf':  ('bi-file-earmark-pdf',    '#ef4444'),
    'doc':  ('bi-file-earmark-word',   '#2563eb'),
    'docx': ('bi-file-earmark-word',   '#2563eb'),
    'odt':  ('bi-file-earmark-word',   '#2563eb'),
    'txt':  ('bi-file-earmark-text',   '#64748b'),
    'md':   ('bi-file-earmark-text',   '#64748b'),
    'rtf':  ('bi-file-earmark-text',   '#64748b'),
    # Spreadsheets
    'xls':  ('bi-file-earmark-excel',  '#16a34a'),
    'xlsx': ('bi-file-earmark-excel',  '#16a34a'),
    'csv':  ('bi-file-earmark-excel',  '#16a34a'),
    'ods':  ('bi-file-earmark-excel',  '#16a34a'),
    # Presentations
    'ppt':  ('bi-file-earmark-slides', '#ea580c'),
    'pptx': ('bi-file-earmark-slides', '#ea580c'),
    'odp':  ('bi-file-earmark-slides', '#ea580c'),
    # Images
    'jpg':  ('bi-file-earmark-image',  '#7c3aed'),
    'jpeg': ('bi-file-earmark-image',  '#7c3aed'),
    'png':  ('bi-file-earmark-image',  '#7c3aed'),
    'gif':  ('bi-file-earmark-image',  '#7c3aed'),
    'webp': ('bi-file-earmark-image',  '#7c3aed'),
    'svg':  ('bi-file-earmark-image',  '#7c3aed'),
    'bmp':  ('bi-file-earmark-image',  '#7c3aed'),
    'tiff': ('bi-file-earmark-image',  '#7c3aed'),
    # Video
    'mp4':  ('bi-file-earmark-play',   '#dc2626'),
    'mov':  ('bi-file-earmark-play',   '#dc2626'),
    'avi':  ('bi-file-earmark-play',   '#dc2626'),
    'mkv':  ('bi-file-earmark-play',   '#dc2626'),
    'webm': ('bi-file-earmark-play',   '#dc2626'),
    # Audio
    'mp3':  ('bi-file-earmark-music',  '#db2777'),
    'wav':  ('bi-file-earmark-music',  '#db2777'),
    'ogg':  ('bi-file-earmark-music',  '#db2777'),
    'm4a':  ('bi-file-earmark-music',  '#db2777'),
    # Archives
    'zip':  ('bi-file-earmark-zip',    '#d97706'),
    'rar':  ('bi-file-earmark-zip',    '#d97706'),
    '7z':   ('bi-file-earmark-zip',    '#d97706'),
    'tar':  ('bi-file-earmark-zip',    '#d97706'),
    'gz':   ('bi-file-earmark-zip',    '#d97706'),
    # Code
    'py':   ('bi-file-earmark-code',   '#0891b2'),
    'js':   ('bi-file-earmark-code',   '#0891b2'),
    'html': ('bi-file-earmark-code',   '#0891b2'),
    'css':  ('bi-file-earmark-code',   '#0891b2'),
    'json': ('bi-file-earmark-code',   '#0891b2'),
    'xml':  ('bi-file-earmark-code',   '#0891b2'),
    'sql':  ('bi-file-earmark-code',   '#0891b2'),
}

_TYPE_CATEGORY = {
    'document': {'doc', 'docx', 'pdf', 'txt', 'md', 'rtf', 'odt'},
    'spreadsheet': {'xls', 'xlsx', 'csv', 'ods'},
    'presentation': {'ppt', 'pptx', 'odp'},
    'image': {'jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bmp', 'tiff'},
    'video': {'mp4', 'mov', 'avi', 'mkv', 'webm'},
    'audio': {'mp3', 'wav', 'ogg', 'm4a'},
    'archive': {'zip', 'rar', '7z', 'tar', 'gz'},
    'code': {'py', 'js', 'html', 'css', 'json', 'xml', 'sql'},
}


class SharedFile(models.Model):
    class Visibility(models.TextChoices):
        PRIVATE = 'private', 'Private'
        SHARED_WITH = 'shared_with', 'Specific People'
        UNIT = 'unit', 'Unit'
        DEPARTMENT = 'department', 'Department'
        OFFICE = 'office', 'Office-wide'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    file = models.FileField(upload_to='shared_files/%Y/%m/')
    folder = models.ForeignKey(FileFolder, on_delete=models.SET_NULL, null=True, blank=True, related_name='files')
    uploaded_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='uploaded_files')
    visibility = models.CharField(max_length=20, choices=Visibility.choices, default=Visibility.PRIVATE)
    shared_with = models.ManyToManyField(User, related_name='files_shared_with', blank=True)
    unit = models.ForeignKey('organization.Unit', on_delete=models.SET_NULL, null=True, blank=True)
    department = models.ForeignKey('organization.Department', on_delete=models.SET_NULL, null=True, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)
    file_type = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)
    tags = models.CharField(max_length=500, blank=True)
    download_count = models.PositiveIntegerField(default=0)
    version = models.PositiveSmallIntegerField(default=1)
    is_latest = models.BooleanField(default=True)
    previous_version = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    @property
    def extension(self):
        _, ext = os.path.splitext(self.name)
        return ext.lower().lstrip('.')

    @property
    def icon_class(self):
        return _EXT_MAP.get(self.extension, ('bi-file-earmark', '#64748b'))[0]

    @property
    def icon_color(self):
        return _EXT_MAP.get(self.extension, ('bi-file-earmark', '#64748b'))[1]

    @property
    def type_category(self):
        ext = self.extension
        for cat, exts in _TYPE_CATEGORY.items():
            if ext in exts:
                return cat
        return 'other'

    @property
    def is_image(self):
        return self.extension in {'jpg', 'jpeg', 'png', 'gif', 'webp', 'svg'}

    @property
    def size_display(self):
        size = self.file_size
        for unit in ('B', 'KB', 'MB', 'GB'):
            if size < 1024:
                return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
            size /= 1024
        return f'{size:.1f} TB'

    @property
    def tag_list(self):
        return [t.strip() for t in self.tags.split(',') if t.strip()]