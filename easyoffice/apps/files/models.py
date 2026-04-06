import os, uuid, hashlib, json
from django.db import models
from django.utils import timezone
from apps.core.models import User


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
        """Recompute top-level status from signers."""
        signers = list(self.signers.all())
        if not signers:
            return
        statuses = {s.status for s in signers}
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


class SignatureRequestSigner(models.Model):
    class Status(models.TextChoices):
        PENDING  = 'pending',  'Pending'
        VIEWED   = 'viewed',   'Viewed'
        SIGNED   = 'signed',   'Signed'
        DECLINED = 'declined', 'Declined'

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