import uuid
from django.db import models
from django.utils.translation import gettext_lazy as _
from apps.core.models import User


class ChatRoom(models.Model):
    class RoomType(models.TextChoices):
        DIRECT       = 'direct',       _('Direct Message')
        GROUP        = 'group',        _('Group Chat')
        UNIT         = 'unit',         _('Unit Channel')
        DEPARTMENT   = 'department',   _('Department Channel')
        ANNOUNCEMENT = 'announcement', _('Announcements')
        PROJECT      = 'project',      _('Project Channel')

    id             = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name           = models.CharField(max_length=200, blank=True)
    room_type      = models.CharField(max_length=20, choices=RoomType.choices, default=RoomType.GROUP)
    members        = models.ManyToManyField(User, related_name='chat_rooms', through='ChatRoomMember')
    created_by     = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_rooms')
    description    = models.TextField(blank=True)
    avatar         = models.ImageField(upload_to='chat_rooms/', null=True, blank=True)
    is_archived    = models.BooleanField(default=False)
    is_readonly    = models.BooleanField(default=False)
    pinned_message = models.ForeignKey(
        'ChatMessage', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+'
    )
    unit           = models.ForeignKey('organization.Unit',       on_delete=models.SET_NULL, null=True, blank=True)
    department     = models.ForeignKey('organization.Department', on_delete=models.SET_NULL, null=True, blank=True)
    project        = models.ForeignKey('projects.Project',        on_delete=models.SET_NULL, null=True, blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.name or f'Room {self.id}'

    @property
    def last_message(self):
        return self.messages.filter(is_deleted=False).first()


class ChatRoomMember(models.Model):
    class Role(models.TextChoices):
        ADMIN    = 'admin',    _('Admin')
        MEMBER   = 'member',   _('Member')
        READONLY = 'readonly', _('Read Only')

    room      = models.ForeignKey(ChatRoom, on_delete=models.CASCADE)
    user      = models.ForeignKey(User,     on_delete=models.CASCADE)
    role      = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)
    last_read = models.DateTimeField(null=True, blank=True)
    is_muted  = models.BooleanField(default=False)
    is_pinned = models.BooleanField(default=False)

    class Meta:
        unique_together = ['room', 'user']

    def __str__(self):
        return f'{self.user} in {self.room}'


class ChatMessage(models.Model):
    class MessageType(models.TextChoices):
        TEXT   = 'text',   _('Text')
        FILE   = 'file',   _('File')
        IMAGE  = 'image',  _('Image')
        SYSTEM = 'system', _('System')
        POLL = 'poll', _('Poll')
        VOICE  = 'voice',  _('Voice Note')

    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    room         = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name='messages')
    sender       = models.ForeignKey(User,     on_delete=models.CASCADE, related_name='chat_messages')
    message_type = models.CharField(max_length=20, choices=MessageType.choices, default=MessageType.TEXT)
    content      = models.TextField(blank=True)

    # Direct device upload
    file      = models.FileField(upload_to='chat_files/%Y/%m/', null=True, blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    file_size = models.PositiveIntegerField(default=0)

    # ── Reference to a file from the Files app (no re-upload) ──────────────
    linked_file = models.ForeignKey(
        'files.SharedFile',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='chat_messages',
        help_text='Attach an existing file from the Files app without re-uploading.',
    )
    # ───────────────────────────────────────────────────────────────────────

    reply_to   = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='replies')
    is_edited  = models.BooleanField(default=False)
    edited_at  = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    reads      = models.ManyToManyField(User, related_name='read_messages', blank=True)
    reactions  = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes  = [models.Index(fields=['room', 'created_at'])]

    def __str__(self):
        return f'{self.sender}: {self.content[:50]}'

    # ── Unified file accessors ──────────────────────────────────────────────
    @property
    def effective_file_url(self):
        """File URL regardless of whether it's a direct upload or linked file."""
        if self.file:
            try:
                return self.file.url
            except Exception:
                pass
        if self.linked_file_id:
            try:
                lf = self.linked_file
                if lf and lf.file:
                    return lf.file.url
            except Exception:
                pass
        return ''

    @property
    def effective_file_name(self):
        if self.file_name:
            return self.file_name
        if self.linked_file_id:
            try:
                lf = self.linked_file
                if lf:
                    return lf.name
            except Exception:
                pass
        return ''

    @property
    def effective_file_size(self):
        if self.file_size:
            return self.file_size
        if self.linked_file_id:
            try:
                lf = self.linked_file
                if lf:
                    return lf.file_size
            except Exception:
                pass
        return 0


class ChatRoomFile(models.Model):
    """
    Files explicitly pinned to a chat room by the room manager.
    Grants every room member download access regardless of their
    normal SharedFile visibility permissions.
    """
    room = models.ForeignKey(
        'ChatRoom',
        on_delete=models.CASCADE,
        related_name='room_files',
    )
    file = models.ForeignKey(
        'files.SharedFile',
        on_delete=models.CASCADE,
        related_name='room_shares',
    )
    shared_by = models.ForeignKey(
        'core.User',
        on_delete=models.SET_NULL,
        null=True,
        related_name='room_file_shares',
    )
    note = models.CharField(max_length=200, blank=True)
    shared_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('room', 'file')]
        ordering = ['-shared_at']

    def __str__(self):
        return f'{self.file.name} → {self.room.name}'

class ChatPoll(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.OneToOneField(
        ChatMessage,
        on_delete=models.CASCADE,
        related_name='poll'
    )
    question = models.CharField(max_length=300)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.question


class ChatPollOption(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    poll = models.ForeignKey(ChatPoll, on_delete=models.CASCADE, related_name='options')
    text = models.CharField(max_length=200)

    def __str__(self):
        return self.text

    @property
    def vote_count(self):
        return self.votes.count()


class ChatPollVote(models.Model):
    poll = models.ForeignKey(ChatPoll, on_delete=models.CASCADE, related_name='votes')
    option = models.ForeignKey(ChatPollOption, on_delete=models.CASCADE, related_name='votes')
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    voted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['poll', 'user']