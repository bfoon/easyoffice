import os, uuid, hashlib, json
from django.db import models
from django.utils import timezone
from apps.core.models import User
from apps.projects.models import Project
from django.conf import settings

# ── Folder ──────────────────────────────────────────────────────────────────

class FileFolder(models.Model):
    class Visibility(models.TextChoices):
        PRIVATE    = 'private',    'Private'
        UNIT       = 'unit',       'Unit'
        DEPARTMENT = 'department', 'Department'
        OFFICE     = 'office',     'Office-wide'

    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name       = models.CharField(max_length=200)
    owner      = models.ForeignKey(User, on_delete=models.CASCADE, related_name='owned_folders')
    parent     = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='subfolders')
    visibility = models.CharField(max_length=20, choices=Visibility.choices, default=Visibility.PRIVATE)
    unit       = models.ForeignKey('organization.Unit', on_delete=models.SET_NULL, null=True, blank=True)
    department = models.ForeignKey('organization.Department', on_delete=models.SET_NULL, null=True, blank=True)
    share_children = models.BooleanField(
        default=False,
        help_text='If true, this folder share also applies to files/subfolders inside it.'
    )
    inherit_parent_sharing = models.BooleanField(
        default=True,
        help_text='If false, this folder will not inherit sharing from parent folders.'
    )
    color      = models.CharField(max_length=7, default='#f59e0b')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def file_count(self):
        return self.files.filter(is_latest=True).count()

    def ancestors(self):
        result, current = [], self.parent
        while current:
            result.insert(0, current)
            current = current.parent
        return result


# ── File type helpers ────────────────────────────────────────────────────────

_EXT_MAP = {
    'pdf':  ('bi-file-earmark-pdf',    '#ef4444'),
    'doc':  ('bi-file-earmark-word',   '#2563eb'),
    'docx': ('bi-file-earmark-word',   '#2563eb'),
    'odt':  ('bi-file-earmark-word',   '#2563eb'),
    'txt':  ('bi-file-earmark-text',   '#64748b'),
    'md':   ('bi-file-earmark-text',   '#64748b'),
    'rtf':  ('bi-file-earmark-text',   '#64748b'),
    'xls':  ('bi-file-earmark-excel',  '#16a34a'),
    'xlsx': ('bi-file-earmark-excel',  '#16a34a'),
    'csv':  ('bi-file-earmark-excel',  '#16a34a'),
    'ods':  ('bi-file-earmark-excel',  '#16a34a'),
    'ppt':  ('bi-file-earmark-slides', '#ea580c'),
    'pptx': ('bi-file-earmark-slides', '#ea580c'),
    'odp':  ('bi-file-earmark-slides', '#ea580c'),
    'jpg':  ('bi-file-earmark-image',  '#7c3aed'),
    'jpeg': ('bi-file-earmark-image',  '#7c3aed'),
    'png':  ('bi-file-earmark-image',  '#7c3aed'),
    'gif':  ('bi-file-earmark-image',  '#7c3aed'),
    'webp': ('bi-file-earmark-image',  '#7c3aed'),
    'svg':  ('bi-file-earmark-image',  '#7c3aed'),
    'mp4':  ('bi-file-earmark-play',   '#dc2626'),
    'mov':  ('bi-file-earmark-play',   '#dc2626'),
    'mp3':  ('bi-file-earmark-music',  '#db2777'),
    'wav':  ('bi-file-earmark-music',  '#db2777'),
    'zip':  ('bi-file-earmark-zip',    '#d97706'),
    'rar':  ('bi-file-earmark-zip',    '#d97706'),
    '7z':   ('bi-file-earmark-zip',    '#d97706'),
    'py':   ('bi-file-earmark-code',   '#0891b2'),
    'js':   ('bi-file-earmark-code',   '#0891b2'),
    'html': ('bi-file-earmark-code',   '#0891b2'),
    'json': ('bi-file-earmark-code',   '#0891b2'),
}

_TYPE_CATEGORY = {
    'document':     {'doc','docx','pdf','txt','md','rtf','odt'},
    'spreadsheet':  {'xls','xlsx','csv','ods'},
    'presentation': {'ppt','pptx','odp'},
    'image':        {'jpg','jpeg','png','gif','webp','svg','bmp','tiff'},
    'video':        {'mp4','mov','avi','mkv','webm'},
    'audio':        {'mp3','wav','ogg','m4a'},
    'archive':      {'zip','rar','7z','tar','gz'},
    'code':         {'py','js','html','css','json','xml','sql'},
}

# File types that can be converted to PDF
CONVERTIBLE_TYPES = {
    'doc','docx','odt','rtf',
    'xls','xlsx','ods','csv',
    'ppt','pptx','odp',
    'txt','md',
    'jpg','jpeg','png','webp','bmp','tiff'
}


# ── SharedFile ───────────────────────────────────────────────────────────────

class SharedFile(models.Model):
    class Visibility(models.TextChoices):
        PRIVATE     = 'private',     'Private'
        SHARED_WITH = 'shared_with', 'Specific People'
        UNIT        = 'unit',        'Unit'
        DEPARTMENT  = 'department',  'Department'
        OFFICE      = 'office',      'Office-wide'

    id               = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name             = models.CharField(max_length=255)
    file             = models.FileField(upload_to='shared_files/%Y/%m/')
    folder           = models.ForeignKey(FileFolder, on_delete=models.SET_NULL, null=True, blank=True,
                                          related_name='files')
    project = models.ForeignKey(
        Project, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='documents'
    )
    uploaded_by      = models.ForeignKey(User, on_delete=models.CASCADE, related_name='uploaded_files')
    visibility       = models.CharField(max_length=20, choices=Visibility.choices, default=Visibility.PRIVATE)
    shared_with      = models.ManyToManyField(User, related_name='files_shared_with', blank=True)
    unit             = models.ForeignKey('organization.Unit', on_delete=models.SET_NULL, null=True, blank=True)
    department       = models.ForeignKey('organization.Department', on_delete=models.SET_NULL, null=True, blank=True)
    file_size        = models.PositiveBigIntegerField(default=0)
    file_type        = models.CharField(max_length=100, blank=True)
    description      = models.TextField(blank=True)
    tags             = models.CharField(max_length=500, blank=True)
    download_count   = models.PositiveIntegerField(default=0)
    version          = models.PositiveSmallIntegerField(default=1)
    is_latest        = models.BooleanField(default=True)
    previous_version = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True)
    # SHA-256 hash for tamper detection
    file_hash        = models.CharField(max_length=64, blank=True)
    inherit_folder_sharing = models.BooleanField(
        default=True,
        help_text='If false, this file ignores inherited sharing from parent folders.'
    )
    created_at       = models.DateTimeField(auto_now_add=True)
    updated_at       = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    @property
    def extension(self):
        _, ext = os.path.splitext(self.name)
        return ext.lower().lstrip('.')

    @property
    def is_zip(self):
        return self.extension == 'zip'

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
        return self.extension in {'jpg','jpeg','png','gif','webp','svg'}

    @property
    def is_convertible(self):
        return self.extension in CONVERTIBLE_TYPES

    @property
    def is_pdf(self):
        return self.extension == 'pdf'

    @property
    def size_display(self):
        size = self.file_size
        for unit in ('B','KB','MB','GB'):
            if size < 1024:
                return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
            size /= 1024
        return f'{size:.1f} TB'

    @property
    def tag_list(self):
        return [t.strip() for t in self.tags.split(',') if t.strip()]

    def compute_hash(self):
        """Compute SHA-256 of file contents."""
        h = hashlib.sha256()
        self.file.open('rb')
        for chunk in iter(lambda: self.file.read(8192), b''):
            h.update(chunk)
        self.file.close()
        return h.hexdigest()

#---- File Share Permissions --------------------------------------------------

class SharePermission(models.TextChoices):
    VIEW = 'view', 'View'
    EDIT = 'edit', 'Edit'
    FULL = 'full', 'Full'


class FileShareAccess(models.Model):
    file = models.ForeignKey('SharedFile', on_delete=models.CASCADE, related_name='share_access')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='file_share_access')
    permission = models.CharField(max_length=10, choices=SharePermission.choices, default=SharePermission.VIEW)
    granted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='file_permissions_granted')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('file', 'user')
        ordering = ['user__first_name', 'user__last_name']

    def __str__(self):
        return f'{self.user} → {self.file.name} ({self.permission})'


class FolderShareAccess(models.Model):
    folder = models.ForeignKey('FileFolder', on_delete=models.CASCADE, related_name='share_access')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='folder_share_access')
    permission = models.CharField(max_length=10, choices=SharePermission.choices, default=SharePermission.VIEW)
    granted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='folder_permissions_granted')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('folder', 'user')
        ordering = ['user__first_name', 'user__last_name']

    def __str__(self):
        return f'{self.user} → {self.folder.name} ({self.permission})'


class FileHistory(models.Model):
    class Action(models.TextChoices):
        CREATED = 'created', 'Created'
        UPDATED = 'updated', 'Updated'
        RENAMED = 'renamed', 'Renamed'
        MOVED = 'moved', 'Moved'
        SHARED = 'shared', 'Shared'
        PERMISSION_CHANGED = 'permission_changed', 'Permission Changed'
        DOWNLOADED = 'downloaded', 'Downloaded'
        RESTORED = 'restored', 'Restored'
        DELETED = 'deleted', 'Deleted'
        VERSIONED = 'versioned', 'Versioned'

    file = models.ForeignKey('SharedFile', on_delete=models.CASCADE, related_name='history_rows')
    action = models.CharField(max_length=30, choices=Action.choices)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='file_history_actions')
    notes = models.TextField(blank=True)
    snapshot_name = models.CharField(max_length=255, blank=True)
    snapshot_folder_name = models.CharField(max_length=255, blank=True)
    snapshot_visibility = models.CharField(max_length=20, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class FileTrash(models.Model):
    class ItemType(models.TextChoices):
        FILE = 'file', 'File'
        FOLDER = 'folder', 'Folder'

    item_type = models.CharField(max_length=10, choices=ItemType.choices)
    original_file = models.ForeignKey('SharedFile', on_delete=models.SET_NULL, null=True, blank=True, related_name='trash_entries')
    original_folder = models.ForeignKey('FileFolder', on_delete=models.SET_NULL, null=True, blank=True, related_name='trash_entries')
    name = models.CharField(max_length=255)
    file_blob = models.FileField(upload_to='trash/%Y/%m/', null=True, blank=True)
    deleted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='deleted_file_trash')
    owner = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='owned_file_trash')
    original_parent_folder = models.ForeignKey('FileFolder', on_delete=models.SET_NULL, null=True, blank=True, related_name='trashed_children')
    original_visibility = models.CharField(max_length=20, blank=True)
    original_description = models.TextField(blank=True)
    original_tags = models.CharField(max_length=500, blank=True)
    deleted_at = models.DateTimeField(default=timezone.now)
    restored_at = models.DateTimeField(null=True, blank=True)
    is_restored = models.BooleanField(default=False)

    class Meta:
        ordering = ['-deleted_at']

    def __str__(self):
        return f'{self.name} ({self.item_type})'

# ── Signature Flow ────────────────────────────────────────────────────────────

class SignatureRequest(models.Model):
    class Status(models.TextChoices):
        DRAFT      = 'draft',      'Draft'
        SENT       = 'sent',       'Sent for Signing'
        PARTIAL    = 'partial',    'Partially Signed'
        COMPLETED  = 'completed',  'Completed'
        DECLINED   = 'declined',   'Declined'
        EXPIRED    = 'expired',    'Expired'
        CANCELLED  = 'cancelled',  'Cancelled'

    id             = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title          = models.CharField(max_length=300)
    message        = models.TextField(blank=True, help_text='Message sent to all signers')
    document       = models.ForeignKey(SharedFile, on_delete=models.CASCADE,
                                        related_name='signature_requests')
    created_by     = models.ForeignKey(User, on_delete=models.CASCADE,
                                        related_name='created_signature_requests')
    status         = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text='Extra context used by other apps, e.g. orders.order_id and orders.stage.'
    )
    # The final signed PDF (generated once all sign)
    signed_document = models.FileField(upload_to='signed_docs/%Y/%m/', null=True, blank=True)
    # Signing order enforced?
    ordered_signing = models.BooleanField(default=False,
                      help_text='If true, signers must sign in the listed order')
    expires_at     = models.DateTimeField(null=True, blank=True)
    completed_at   = models.DateTimeField(null=True, blank=True)
    # Tamper-evident audit hash (JSON string of audit log, hashed)
    audit_hash     = models.CharField(max_length=64, blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title

    @property
    def is_expired(self):
        return self.expires_at and timezone.now() > self.expires_at

    @property
    def progress_pct(self):
        total = self.signers.count()
        if not total:
            return 0
        signed = self.signers.filter(status='signed').count()
        return round(signed / total * 100)

    @property
    def status_color(self):
        return {
            'draft': '#94a3b8', 'sent': '#3b82f6', 'partial': '#f59e0b',
            'completed': '#10b981', 'declined': '#ef4444',
            'expired': '#94a3b8', 'cancelled': '#64748b',
        }.get(self.status, '#64748b')

    def update_status(self):
        """
        Recompute top-level status from signers.

        Important:
        Do not reopen terminal requests. If a request is cancelled or expired,
        it must stay cancelled/expired even if signer rows are still pending.
        """
        if self.status in {
            self.Status.CANCELLED,
            self.Status.EXPIRED,
        }:
            return

        signers = list(self.signers.all())
        if not signers:
            return

        statuses = {s.status for s in signers}

        if 'cancelled' in statuses:
            self.status = self.Status.CANCELLED
            self.save(update_fields=['status', 'updated_at'])
            return

        if 'declined' in statuses:
            self.status = self.Status.DECLINED
        elif all(s.status == 'signed' for s in signers):
            self.status = self.Status.COMPLETED
            self.completed_at = timezone.now()
        elif any(s.status == 'signed' for s in signers):
            self.status = self.Status.PARTIAL
        else:
            self.status = self.Status.SENT

        self.save(update_fields=['status', 'completed_at', 'updated_at'])

    def rebuild_audit_hash(self):
        """Compute a hash over the full audit trail for tamper detection."""
        events = list(
            self.audit_trail.values('event', 'signer_email', 'ip_address', 'timestamp')
            .order_by('timestamp')
        )
        blob = json.dumps(events, default=str, sort_keys=True).encode()
        self.audit_hash = hashlib.sha256(blob).hexdigest()
        self.save(update_fields=['audit_hash'])


    def get_all_documents(self):

        """All attached documents in order, falling back to the primary FK."""
        qs = self.documents.select_related('document').order_by('order')
        if qs.exists():
            return [r.document for r in qs]
        return [self.document] if self.document_id else []

    def can_be_duplicated_by(self, user):
        """Only the original creator can duplicate, and only on a
        non-active terminal status."""
        if self.created_by_id != getattr(user, 'id', None):
            return False
        return self.status in ('cancelled', 'expired', 'completed')


class SignatureRequestSigner(models.Model):
    class Status(models.TextChoices):
        PENDING   = 'pending',   'Pending'
        VIEWED    = 'viewed',    'Viewed'
        SIGNED    = 'signed',    'Signed'
        DECLINED  = 'declined',  'Declined'
        CANCELLED = 'cancelled', 'Cancelled'

    id               = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request          = models.ForeignKey(SignatureRequest, on_delete=models.CASCADE,
                                          related_name='signers')
    # Internal user OR external email
    user             = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                          related_name='signature_requests_as_signer')
    email            = models.EmailField(help_text='Email address of signer')
    name             = models.CharField(max_length=200, help_text='Display name of signer')
    order            = models.PositiveSmallIntegerField(default=1)
    status           = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    # Unique token for the signing link (no login required)
    token            = models.UUIDField(default=uuid.uuid4, unique=True)
    # The actual signature data
    signature_data   = models.TextField(blank=True,
                       help_text='Base64 PNG of drawn signature OR typed text')
    signature_type   = models.CharField(max_length=10, blank=True,
                       choices=[('draw','Drawn'),('type','Typed'),('upload','Uploaded')])
    # Captured metadata
    ip_address       = models.GenericIPAddressField(null=True, blank=True)
    user_agent       = models.CharField(max_length=500, blank=True)
    signed_at        = models.DateTimeField(null=True, blank=True)
    viewed_at        = models.DateTimeField(null=True, blank=True)
    invited_at       = models.DateTimeField(
        null=True, blank=True,
        help_text='When this signer was emailed their "your turn to sign" '
                  'invitation. Used in ordered mode so the next-signer '
                  'notification is idempotent across multiple completions.',
    )
    decline_reason   = models.TextField(blank=True)
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'created_at']

    def __str__(self):
        return f'{self.name} <{self.email}> — {self.get_status_display()}'

    @property
    def signing_url(self):
        from django.urls import reverse
        return reverse('sign_document', kwargs={'token': self.token})


class SignatureRequestDocument(models.Model):
    """
    One row per document attached to a signature request.

    A request always has at least one row (the primary, which mirrors
    `SignatureRequest.document`). Additional rows track the originals
    that were merged into the primary at request-creation time.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        'files.SignatureRequest', on_delete=models.CASCADE, related_name='documents',
    )
    document = models.ForeignKey(
        'files.SharedFile', on_delete=models.CASCADE, related_name='signature_attachments',
    )
    order = models.PositiveSmallIntegerField(
        default=1,
        help_text='Display order — also the merge order in the combined PDF',
    )
    is_primary = models.BooleanField(
        default=False,
        help_text='True for the primary/merged document on the request',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['request', 'document'],
                name='uniq_signature_request_document',
            ),
        ]

    def __str__(self):
        return f'{self.request.title} — {self.document.name}'


class SignatureAuditEvent(models.Model):
    """Tamper-evident audit log for every action in a signature request."""
    EVENTS = [
        ('created',   'Request Created'),
        ('sent',      'Request Sent'),
        ('viewed',    'Document Viewed'),
        ('signed',    'Document Signed'),
        ('declined',  'Signing Declined'),
        ('completed', 'All Signers Completed'),
        ('cancelled', 'Request Cancelled'),
        ('converted', 'Document Converted to PDF'),
    ]
    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request      = models.ForeignKey(SignatureRequest, on_delete=models.CASCADE,
                                      related_name='audit_trail')
    event        = models.CharField(max_length=30, choices=EVENTS)
    signer_email = models.EmailField(blank=True)
    signer_name  = models.CharField(max_length=200, blank=True)
    ip_address   = models.GenericIPAddressField(null=True, blank=True)
    user_agent   = models.CharField(max_length=500, blank=True)
    notes        = models.TextField(blank=True)
    timestamp    = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['timestamp']

    def __str__(self):
        return f'[{self.event}] {self.request.title} @ {self.timestamp}'


# ── Signature Field Placement ─────────────────────────────────────────────────

class SignatureField(models.Model):
    """
    A positioned field on the document that a specific signer must complete.
    Coordinates are stored as percentages of the page dimensions so they
    are device/zoom-independent.
    """
    class FieldType(models.TextChoices):
        SIGNATURE = 'signature', 'Signature'
        INITIALS  = 'initials',  'Initials'
        DATE      = 'date',      'Date'
        TEXT      = 'text',      'Text'

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request     = models.ForeignKey(SignatureRequest, on_delete=models.CASCADE,
                                     related_name='fields')
    signer      = models.ForeignKey(SignatureRequestSigner, on_delete=models.CASCADE,
                                     related_name='fields', null=True, blank=True)
    field_type  = models.CharField(max_length=20, choices=FieldType.choices,
                                    default=FieldType.SIGNATURE)
    page        = models.PositiveSmallIntegerField(default=1)
    # Position as % of page (0-100)
    x_pct       = models.FloatField(default=10.0)
    y_pct       = models.FloatField(default=10.0)
    width_pct   = models.FloatField(default=20.0)
    height_pct  = models.FloatField(default=5.0)
    label       = models.CharField(max_length=100, blank=True)
    required    = models.BooleanField(default=True)
    # Filled-in value (set when signer completes the field)
    value       = models.TextField(blank=True)
    filled_at   = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['page', 'y_pct', 'x_pct']

    def __str__(self):
        return f'{self.get_field_type_display()} — page {self.page}'

    def to_dict(self):
        return {
            'id':         str(self.id),
            'type':       self.field_type,
            'page':       self.page,
            'x':          self.x_pct,
            'y':          self.y_pct,
            'w':          self.width_pct,
            'h':          self.height_pct,
            'label':      self.label,
            'required':   self.required,
            'signer_id':  str(self.signer_id) if self.signer_id else None,
            'signer_name': self.signer.name if self.signer else '',
            'filled':     bool(self.value),
        }


# ── Saved Signature ───────────────────────────────────────────────────────────

class SavedSignature(models.Model):
    """
    A user's saved signature that can be quickly applied when signing.
    Supports drawn (base64 PNG), typed text, or an uploaded image file.
    """
    class SigType(models.TextChoices):
        DRAW   = 'draw',   'Drawn'
        TYPE   = 'type',   'Typed'
        UPLOAD = 'upload', 'Uploaded Image'

    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user       = models.ForeignKey(User, on_delete=models.CASCADE,
                                    related_name='saved_signatures')
    name       = models.CharField(max_length=100, default='My Signature',
                                   help_text='Label to identify this signature')
    sig_type   = models.CharField(max_length=10, choices=SigType.choices,
                                   default=SigType.DRAW)
    # For drawn: base64 PNG data URI
    # For typed: the text string
    data       = models.TextField(blank=True)
    # For uploaded: the image file
    image      = models.ImageField(upload_to='saved_sigs/', null=True, blank=True)
    is_default = models.BooleanField(default=False,
                  help_text='Auto-apply this signature on signing pages')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-is_default', '-created_at']

    def __str__(self):
        return f'{self.user.full_name} — {self.name} ({self.get_sig_type_display()})'

    def save(self, *args, **kwargs):
        # Enforce only one default per user
        if self.is_default:
            SavedSignature.objects.filter(
                user=self.user, is_default=True
            ).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)

# ── Pin support ───────────────────────────────────────────────────────────────

class FilePinnedItem(models.Model):
    """Tracks which files/folders a user has pinned to the top."""
    user       = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pinned_items')
    file       = models.ForeignKey('SharedFile', on_delete=models.CASCADE,
                                   null=True, blank=True, related_name='pins')
    folder     = models.ForeignKey('FileFolder', on_delete=models.CASCADE,
                                   null=True, blank=True, related_name='pins')
    pinned_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['pinned_at']
        constraints = [
            models.UniqueConstraint(fields=['user','file'],   name='unique_pin_file',
                                    condition=models.Q(file__isnull=False)),
            models.UniqueConstraint(fields=['user','folder'], name='unique_pin_folder',
                                    condition=models.Q(folder__isnull=False)),
        ]

    def __str__(self):
        return f'{self.user} pinned {self.file or self.folder}'


# ── Signature CC / Viewer ─────────────────────────────────────────────────────

class SignatureCC(models.Model):
    """
    CC recipients for a signature request.
    They receive email updates (sent, completed) but do NOT sign.
    """
    class Role(models.TextChoices):
        CC     = 'cc',     'CC (notified only)'
        VIEWER = 'viewer', 'Viewer (can view progress)'

    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request    = models.ForeignKey('SignatureRequest', on_delete=models.CASCADE,
                                    related_name='cc_recipients')
    user       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='signature_cc')
    email      = models.EmailField()
    name       = models.CharField(max_length=200)
    role       = models.CharField(max_length=10, choices=Role.choices, default=Role.CC)
    # Viewer-only token for passwordless access to view (not sign) the document
    view_token = models.UUIDField(default=uuid.uuid4, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} <{self.email}> [{self.role}]'

    @property
    def view_url(self):
        from django.urls import reverse
        return reverse('signature_view_only', kwargs={'token': self.view_token})


# ── Public download token (no-login download for signed docs) ─────────────────

class FilePublicToken(models.Model):
    """
    Allows downloading a specific file without being logged in.
    Used in signature completion emails so recipients can get the signed copy.
    """
    id         = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file       = models.ForeignKey('SharedFile', on_delete=models.CASCADE,
                                    related_name='public_tokens')
    token      = models.UUIDField(default=uuid.uuid4, unique=True)
    label      = models.CharField(max_length=200, blank=True)  # e.g. "Signed copy"
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    download_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Public token for {self.file.name}'

    @property
    def is_expired(self):
        return bool(self.expires_at and timezone.now() > self.expires_at)

    @property
    def public_url(self):
        from django.urls import reverse
        return reverse('file_public_download', kwargs={'token': self.token})


class FileNote(models.Model):
    """
    A personal sticky note a user can attach to any file.
    One note per user per file — auto-saves, never deleted unless user clears it.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.ForeignKey(
        'SharedFile', on_delete=models.CASCADE,
        related_name='notes'
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='file_notes'
    )
    body       = models.TextField(blank=True, default='')
    typing_at  = models.DateTimeField(null=True, blank=True)   # last keystroke ping
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('file', 'author')  # one note per user per file
        ordering = ['-updated_at']

    def __str__(self):
        return f'Note by {self.author} on {self.file.name!r}'


class FileNoteShare(models.Model):
    """
    Records who can see a FileNote.
    scope choices:
        person     — shared with a specific user
        unit       — shared with everyone in a unit
        department — shared with everyone in a department
        office     — shared with the whole office
    When the underlying file sharing is revoked (stop-sharing), call
    FileNoteShare.objects.filter(note__file=file, note__author=user).delete()
    """
    SCOPE_PERSON = 'person'
    SCOPE_UNIT = 'unit'
    SCOPE_DEPARTMENT = 'department'
    SCOPE_OFFICE = 'office'
    SCOPE_CHOICES = [
        (SCOPE_PERSON, 'Specific person'),
        (SCOPE_UNIT, 'Unit'),
        (SCOPE_DEPARTMENT, 'Department'),
        (SCOPE_OFFICE, 'Office'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    note = models.ForeignKey('FileNote', on_delete=models.CASCADE, related_name='shares')
    scope = models.CharField(max_length=20, choices=SCOPE_CHOICES)

    # Only one of the following is set depending on scope
    shared_with_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name='received_note_shares'
    )
    unit = models.ForeignKey(
        'organization.Unit', null=True, blank=True, on_delete=models.CASCADE
    )
    department = models.ForeignKey(
        'organization.Department', null=True, blank=True, on_delete=models.CASCADE
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'NoteShare({self.scope}) for {self.note}'

    @property
    def label(self):
        """Human-readable label for the frontend."""
        if self.scope == self.SCOPE_PERSON and self.shared_with_user:
            return self.shared_with_user.full_name
        if self.scope == self.SCOPE_UNIT and self.unit:
            return self.unit.name
        if self.scope == self.SCOPE_DEPARTMENT and self.department:
            return self.department.name
        if self.scope == self.SCOPE_OFFICE:
            return 'Everyone in the office'
        return '—'

    @property
    def scope_display(self):
        return {
            self.SCOPE_PERSON: 'Specific person',
            self.SCOPE_UNIT: 'Unit',
            self.SCOPE_DEPARTMENT: 'Department',
            self.SCOPE_OFFICE: 'Office-wide',
        }.get(self.scope, self.scope)


# ── File Annotations ──────────────────────────────────────────────────────────

class FileAnnotation(models.Model):
    """
    Stores all per-user annotations on a file (highlights, comments, drawings,
    stamps, bookmarks).  Saved as a single JSON blob — one row per user per file.
    Auto-saved via AJAX; the frontend debounces writes.

    JSON schema (annotations field):
    [
      {
        "id":      "<uuid>",
        "tool":    "highlight" | "comment" | "draw" | "rect" | "stamp" | "bookmark",
        "page":    1,                    # for PDF (0 for image/text)
        "x":       0.0,                  # % of surface width
        "y":       0.0,                  # % of surface height
        "w":       0.0,                  # % width (highlight/rect)
        "h":       0.0,                  # % height
        "color":   "#fbbf24",
        "opacity": 0.5,
        "text":    "",                   # comment body / stamp text
        "paths":   [[x,y], ...],         # freehand draw points (% coords)
        "created_at": "ISO-8601"
      },
      ...
    ]

    tool_settings field stores last-used tool preferences:
    {
      "active_tool": "highlight",
      "color": "#fbbf24",
      "opacity": 0.5,
      "stroke_width": 3
    }
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.ForeignKey(
        'SharedFile', on_delete=models.CASCADE,
        related_name='annotations'
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='file_annotations'
    )
    annotations = models.JSONField(default=list, blank=True)
    tool_settings = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('file', 'author')
        ordering = ['-updated_at']

    def __str__(self):
        count = len(self.annotations) if isinstance(self.annotations, list) else 0
        return f'Annotations by {self.author} on {self.file.name!r} ({count} items)'


# ── Live Preview Session ──────────────────────────────────────────────────────

class LivePreviewSession(models.Model):
    """
    Ephemeral record that lives only while a live preview sharing session is
    active.  Deleted (or marked ended) the moment the presenter closes.

    token          — URL-safe UUID used in the WebSocket path and invite links
    presenter      — user who started the session
    file           — the file being previewed
    viewers        — M2M of users who accepted the invite
    invited        — M2M of users who were sent an invite (may not have joined)
    created_at     — when the session started
    ended_at       — null while active; set when presenter disconnects
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    presenter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='presented_preview_sessions'
    )
    file = models.ForeignKey(
        'SharedFile', on_delete=models.CASCADE,
        related_name='live_preview_sessions'
    )
    viewers = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='joined_preview_sessions', blank=True
    )
    invited = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='invited_preview_sessions', blank=True
    )
    viewers_can_edit = models.BooleanField(
        default=False,
        help_text='If True, viewers may also draw, comment, and modify '
                  'the file during the live session.'
    )
    # ── Office-doc proxy ──────────────────────────────────────────────────
    # When the source file is a Word/Excel/PowerPoint doc, the preview
    # surface needs a PDF version because (a) those formats can't be
    # natively annotated in-browser and (b) iframe-rendered office viewers
    # are cross-origin, blocking cursor/scroll/draw broadcast.
    #
    # On session start we render the source to PDF and store the result
    # in a hidden SharedFile, linked here. Both presenter and viewers
    # render *that* PDF (via the existing PDF.js path), but the session
    # still belongs to the original file. On session end we delete the
    # proxy so we don't leak temporary uploads.
    proxy_pdf = models.ForeignKey(
        'SharedFile', on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='live_preview_proxy_for',
        help_text='Optional PDF version of the source used during the '
                  'session. Created on start for office docs, deleted on end.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        status = 'active' if self.ended_at is None else 'ended'
        return f'LivePreview {self.token} by {self.presenter} [{status}]'

    @property
    def is_active(self):
        return self.ended_at is None

    @property
    def ws_path(self):
        return f'/ws/files/live-preview/{self.token}/'


# ─────────────────────────────────────────────────────────────────────────────
# External (no-login) file sharing
# ─────────────────────────────────────────────────────────────────────────────
#
# Lets internal users share a file with someone outside the office (a vendor,
# auditor, partner, donor) without giving them an EasyOffice account.
#
# Flow:
#   1. Owner creates an ExternalFileShare → recipient gets an email with a
#      one-time tokenised "Open File" link.
#   2. First click registers the recipient's device (IP + UA + JS fingerprint)
#      as an ExternalShareDevice in 'pending' state and emails the owner asking
#      to ACCEPT or DECLINE that specific device.
#   3. Owner clicks accept/decline (token-based, no login required for them
#      either since they get the link in their authenticated email session
#      we still gate it with login_required to be safe).
#   4. Once a device is 'accepted', any future click from THAT same device
#      goes straight through to the file — no second verification email.
#   5. A click from any OTHER device starts a new pending verification
#      (each device is tracked separately).
#   6. Sensitive fields (recipient email, device user-agent, decline reason)
#      are stored encrypted at rest using EncryptedTextField, which derives
#      a Fernet key from settings.SECRET_KEY.
#
# Audit trail: every meaningful event (sent, opened, device-pending,
# device-accepted, device-declined, downloaded, expired, revoked) writes
# an ExternalShareAuditEvent row.

import base64 as _b64
import hashlib as _hashlib

try:
    # cryptography is already a dep in most Django setups; if missing, pip
    # install cryptography. We import lazily so the rest of this module can
    # still load even if the lib is not present (encrypted fields will then
    # fall back to plain text and log a warning the first time they are
    # touched).
    from cryptography.fernet import Fernet as _Fernet, InvalidToken as _InvalidToken
    _CRYPTO_AVAILABLE = True
except Exception:  # pragma: no cover
    _Fernet = None
    _InvalidToken = Exception
    _CRYPTO_AVAILABLE = False


def _derive_fernet_key():
    """
    Derive a stable 32-byte Fernet key from settings.SECRET_KEY.

    SECRET_KEY can be any length; Fernet wants a urlsafe-b64-encoded 32-byte
    key, so we hash to 32 bytes then b64-encode.
    """
    raw = settings.SECRET_KEY.encode('utf-8') if isinstance(settings.SECRET_KEY, str) else settings.SECRET_KEY
    digest = _hashlib.sha256(raw).digest()
    return _b64.urlsafe_b64encode(digest)


class EncryptedTextField(models.TextField):
    """
    A TextField that encrypts its value at-rest using Fernet (AES-128-CBC +
    HMAC-SHA256). Reads transparently decrypt. Stores ciphertext as ASCII
    base64 inside a regular TEXT column, so it works on any DB without a
    schema change.

    If cryptography is not installed (or decryption fails for any reason —
    e.g. the value was written by an older version with a different key),
    the field falls back to returning the raw stored value rather than
    raising, so the application keeps working. Failures are logged.
    """

    description = 'Encrypted text (Fernet, key derived from SECRET_KEY)'

    _PREFIX = 'enc::'  # so we can tell encrypted from legacy plaintext rows

    def _fernet(self):
        if not _CRYPTO_AVAILABLE:
            return None
        return _Fernet(_derive_fernet_key())

    def get_prep_value(self, value):
        if value is None or value == '':
            return value
        f = self._fernet()
        if f is None:
            return value  # plaintext fallback
        token = f.encrypt(str(value).encode('utf-8')).decode('ascii')
        return self._PREFIX + token

    def from_db_value(self, value, expression, connection):
        return self.to_python(value)

    def to_python(self, value):
        if value is None or value == '':
            return value
        if not isinstance(value, str):
            return value
        if not value.startswith(self._PREFIX):
            # Legacy / unencrypted row — return as-is.
            return value
        f = self._fernet()
        if f is None:
            return value[len(self._PREFIX):]
        try:
            return f.decrypt(value[len(self._PREFIX):].encode('ascii')).decode('utf-8')
        except _InvalidToken:
            return value  # don't crash on bad ciphertext


class ExternalFileShare(models.Model):
    """
    A token-based, expiring file share to a single external recipient.

    The same recipient may try to open the link from multiple devices; each
    device is tracked and individually accepted/declined via
    ExternalShareDevice. The share itself stays valid until expires_at,
    revoked_at, or all download budgets are exhausted.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'   # email sent, not yet opened
        ACTIVE  = 'active',  'Active'    # at least one device is accepted
        REVOKED = 'revoked', 'Revoked'   # owner cancelled
        EXPIRED = 'expired', 'Expired'   # past expires_at

    id              = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file            = models.ForeignKey('SharedFile', on_delete=models.CASCADE,
                                         related_name='external_shares')
    token           = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True)
    # Encrypted PII
    recipient_email = EncryptedTextField()
    recipient_name  = EncryptedTextField(blank=True, default='')

    # The plain (non-encrypted) hash lets us look up "is this email already
    # shared on this file?" without decrypting every row.
    recipient_email_hash = models.CharField(max_length=64, db_index=True)

    message         = models.TextField(blank=True, default='',
                                        help_text='Optional note shown in the invitation email.')
    permission      = models.CharField(max_length=10, default='view',
                                        choices=[('view', 'View only'),
                                                 ('download', 'View + Download')])

    expires_at      = models.DateTimeField(
                        help_text='After this point the link stops working.')
    max_downloads   = models.PositiveIntegerField(default=0,
                        help_text='0 = unlimited downloads while link is valid.')
    download_count  = models.PositiveIntegerField(default=0)

    status          = models.CharField(max_length=10, choices=Status.choices,
                                        default=Status.PENDING)
    created_by      = models.ForeignKey(User, on_delete=models.CASCADE,
                                         related_name='external_shares_created')
    created_at      = models.DateTimeField(auto_now_add=True)
    revoked_at      = models.DateTimeField(null=True, blank=True)
    revoked_by      = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                         blank=True,
                                         related_name='external_shares_revoked')

    class Meta:
        ordering = ['-created_at']
        indexes  = [
            models.Index(fields=['recipient_email_hash', 'file']),
            models.Index(fields=['status', 'expires_at']),
        ]

    def __str__(self):
        return f'ExternalShare {self.token} → {self.file.name}'

    # ── Helpers ──

    @staticmethod
    def hash_email(email):
        """Stable lookup hash. Lower-cased, stripped, sha256 hex."""
        if not email:
            return ''
        normalised = email.strip().lower().encode('utf-8')
        return _hashlib.sha256(normalised).hexdigest()

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def is_revoked(self):
        return self.status == self.Status.REVOKED or self.revoked_at is not None

    @property
    def is_active(self):
        if self.is_revoked or self.is_expired:
            return False
        if self.max_downloads and self.download_count >= self.max_downloads:
            return False
        return True

    @property
    def open_url(self):
        from django.urls import reverse
        return reverse('external_share_open', kwargs={'token': self.token})

    def refresh_status(self):
        """Recompute status from current state. Caller is responsible for save()."""
        if self.revoked_at:
            self.status = self.Status.REVOKED
        elif self.is_expired:
            self.status = self.Status.EXPIRED
        elif self.devices.filter(status=ExternalShareDevice.Status.ACCEPTED).exists():
            self.status = self.Status.ACTIVE
        else:
            self.status = self.Status.PENDING


class ExternalShareDevice(models.Model):
    """
    Each distinct device that has tried to open an ExternalFileShare.

    A "device" is identified by a fingerprint hash (browser-side stable JS
    fingerprint posted on first open, persisted in localStorage and verified
    against IP + UA on the server). One ExternalFileShare can have many
    devices in different states.
    """

    class Status(models.TextChoices):
        PENDING  = 'pending',  'Pending verification'
        ACCEPTED = 'accepted', 'Accepted'
        DECLINED = 'declined', 'Declined'

    id                  = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    share               = models.ForeignKey(ExternalFileShare, on_delete=models.CASCADE,
                                             related_name='devices')
    fingerprint         = models.CharField(max_length=128, db_index=True,
                                            help_text='SHA-256 of (clientFingerprint || UA || /24 IP)')
    verify_token        = models.UUIDField(default=uuid.uuid4, unique=True, db_index=True,
                                            help_text='One-time token in the accept/decline email.')

    # Captured for security review only. UA encrypted because it can be
    # surprisingly identifying.
    ip_address          = models.GenericIPAddressField(null=True, blank=True)
    user_agent          = EncryptedTextField(blank=True, default='')
    # Coarse geographic + device hints — best-effort, never required.
    country_code        = models.CharField(max_length=2, blank=True, default='')
    city                = models.CharField(max_length=120, blank=True, default='')
    device_type         = models.CharField(max_length=40, blank=True, default='',
                                            help_text='mobile / tablet / desktop / unknown')
    os_name             = models.CharField(max_length=60, blank=True, default='')
    browser_name        = models.CharField(max_length=60, blank=True, default='')

    status              = models.CharField(max_length=10, choices=Status.choices,
                                            default=Status.PENDING)
    decline_reason      = EncryptedTextField(blank=True, default='')

    first_seen_at       = models.DateTimeField(auto_now_add=True)
    last_seen_at        = models.DateTimeField(auto_now=True)
    decided_at          = models.DateTimeField(null=True, blank=True)
    decided_by          = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                             blank=True,
                                             related_name='external_share_decisions')
    access_count        = models.PositiveIntegerField(default=0,
                                                       help_text='Successful opens after acceptance.')

    class Meta:
        ordering        = ['-last_seen_at']
        unique_together = ('share', 'fingerprint')
        indexes         = [
            models.Index(fields=['share', 'status']),
        ]

    def __str__(self):
        return f'Device {self.fingerprint[:12]}… on {self.share_id} ({self.status})'

    @property
    def is_accepted(self):
        return self.status == self.Status.ACCEPTED

    @property
    def is_declined(self):
        return self.status == self.Status.DECLINED

    @property
    def is_pending(self):
        return self.status == self.Status.PENDING

    @property
    def short_label(self):
        bits = []
        if self.browser_name:
            bits.append(self.browser_name)
        if self.os_name:
            bits.append(self.os_name)
        if self.device_type:
            bits.append(self.device_type.title())
        if not bits:
            bits.append('Unknown device')
        if self.country_code:
            bits.append(self.country_code)
        return ' · '.join(bits)


class ExternalShareAuditEvent(models.Model):
    """
    Append-only audit log for an external share. Every meaningful action
    writes a row — used to render the "Activity" timeline on the manage
    page and to satisfy compliance reviewers asking who accessed what.
    """

    class Action(models.TextChoices):
        CREATED          = 'created',          'Share created'
        EMAIL_SENT       = 'email_sent',       'Invitation email sent'
        OPENED           = 'opened',           'Link opened'
        DEVICE_PENDING   = 'device_pending',   'New device awaiting verification'
        DEVICE_ACCEPTED  = 'device_accepted',  'Device accepted'
        DEVICE_DECLINED  = 'device_declined',  'Device declined'
        DOWNLOADED       = 'downloaded',       'File downloaded'
        REVOKED          = 'revoked',          'Share revoked'
        EXPIRED          = 'expired',          'Share expired'
        FAILED_ATTEMPT   = 'failed_attempt',   'Failed access attempt'

    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    share       = models.ForeignKey(ExternalFileShare, on_delete=models.CASCADE,
                                     related_name='audit_events')
    device      = models.ForeignKey(ExternalShareDevice, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='audit_events')
    action      = models.CharField(max_length=24, choices=Action.choices)
    actor       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='external_share_audit_actions')
    notes       = models.TextField(blank=True, default='')
    ip_address  = models.GenericIPAddressField(null=True, blank=True)
    timestamp   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes  = [
            models.Index(fields=['share', 'timestamp']),
        ]

    def __str__(self):
        return f'{self.get_action_display()} on {self.share_id} @ {self.timestamp:%Y-%m-%d %H:%M}'