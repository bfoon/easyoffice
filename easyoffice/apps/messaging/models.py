import uuid
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from apps.core.models import User
from apps.messaging.models_mixin import EncryptedContentMixin, _connect_post_init


class ChatRoom(models.Model):
    class RoomType(models.TextChoices):
        DIRECT = 'direct', _('Direct Message')
        GROUP = 'group', _('Group Chat')
        UNIT = 'unit', _('Unit Channel')
        DEPARTMENT = 'department', _('Department Channel')
        ANNOUNCEMENT = 'announcement', _('Announcements')
        PROJECT = 'project', _('Project Channel')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200, blank=True)
    room_type = models.CharField(max_length=20, choices=RoomType.choices, default=RoomType.GROUP)
    members = models.ManyToManyField(User, related_name='chat_rooms', through='ChatRoomMember')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_rooms')
    description = models.TextField(blank=True)
    avatar = models.ImageField(upload_to='chat_rooms/', null=True, blank=True)
    is_archived = models.BooleanField(default=False)
    is_readonly = models.BooleanField(default=False)
    pinned_message = models.ForeignKey('ChatMessage', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    unit = models.ForeignKey('organization.Unit', on_delete=models.SET_NULL, null=True, blank=True)
    department = models.ForeignKey('organization.Department', on_delete=models.SET_NULL, null=True, blank=True)
    project = models.ForeignKey('projects.Project', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.name or f'Room {self.id}'

    @property
    def last_message(self):
        return self.messages.filter(is_deleted=False).order_by('-created_at').first()


class ChatRoomMember(models.Model):
    class Role(models.TextChoices):
        ADMIN = 'admin', _('Admin')
        MEMBER = 'member', _('Member')
        READONLY = 'readonly', _('Read Only')

    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    last_read = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('room', 'user')]

    def __str__(self):
        return f'{self.user} @ {self.room}'


class ChatMessage(EncryptedContentMixin, models.Model):
    """
    Chat message with transparent at-rest encryption of `content`.

    * `save()` (via EncryptedContentMixin) encrypts `content` before write.
    * `post_init` signal (connected at bottom of this module) decrypts
      `content` automatically on every ORM load.
    """

    class MessageType(models.TextChoices):
        TEXT = 'text', _('Text')
        FILE = 'file', _('File')
        IMAGE = 'image', _('Image')
        SYSTEM = 'system', _('System')
        POLL = 'poll', _('Poll')
        COMMAND = 'command', _('Command')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='sent_chat_messages')
    content = models.TextField(blank=True)
    message_type = models.CharField(max_length=20, choices=MessageType.choices, default=MessageType.TEXT)

    file = models.FileField(upload_to='chat_uploads/', null=True, blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    file_size = models.PositiveBigIntegerField(default=0)

    linked_file = models.ForeignKey('files.SharedFile', on_delete=models.SET_NULL, null=True, blank=True, related_name='linked_chat_messages')
    reply_to = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='replies')

    reactions = models.JSONField(default=dict, blank=True)
    command_payload = models.JSONField(default=dict, blank=True)

    is_deleted = models.BooleanField(default=False)
    is_edited = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.sender} -> {self.room}: {(self.content or "")[:30]}'

    @property
    def effective_file_url(self):
        if self.file:
            try:
                return self.file.url
            except Exception:
                pass
        if self.linked_file_id:
            try:
                if self.linked_file and self.linked_file.file:
                    return self.linked_file.file.url
            except Exception:
                pass
        return ''

    @property
    def effective_file_name(self):
        if self.file_name:
            return self.file_name
        if self.linked_file_id:
            try:
                if self.linked_file:
                    return self.linked_file.name
            except Exception:
                pass
        return ''

    @property
    def effective_file_size(self):
        if self.file_size:
            return self.file_size
        if self.linked_file_id:
            try:
                if self.linked_file:
                    return self.linked_file.file_size
            except Exception:
                pass
        return 0


class ChatMessageMention(models.Model):
    message = models.ForeignKey(ChatMessage, on_delete=models.CASCADE, related_name='mentions')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_mentions')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('message', 'user')]

    def __str__(self):
        return f'@{self.user} in {self.message_id}'


class ChatRoomFile(models.Model):
    room = models.ForeignKey('ChatRoom', on_delete=models.CASCADE, related_name='room_files')
    file = models.ForeignKey('files.SharedFile', on_delete=models.CASCADE, related_name='room_shares')
    shared_by = models.ForeignKey('core.User', on_delete=models.SET_NULL, null=True, related_name='room_file_shares')
    note = models.CharField(max_length=200, blank=True)
    shared_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('room', 'file')]
        ordering = ['-shared_at']

    def __str__(self):
        return f'{self.file.name} → {self.room.name}'


class ChatPoll(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.OneToOneField(ChatMessage, on_delete=models.CASCADE, related_name='poll')
    question = models.CharField(max_length=300)

    allow_multiple = models.BooleanField(default=False)
    is_anonymous = models.BooleanField(default=False)
    allow_vote_change = models.BooleanField(default=True)
    is_closed = models.BooleanField(default=False)
    ends_at = models.DateTimeField(null=True, blank=True)

    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='created_chat_polls')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.question

    @property
    def is_effectively_closed(self):
        if self.is_closed:
            return True
        if self.ends_at and timezone.now() >= self.ends_at:
            return True
        return False


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
        unique_together = [('poll', 'option', 'user')]

    def __str__(self):
        return f'{self.user} -> {self.option}'


# ─────────────────────────────────────────────────────────────────────────────
# Wire up transparent decryption on ORM load
# MUST be at the bottom, AFTER ChatMessage is defined.
# ─────────────────────────────────────────────────────────────────────────────
_connect_post_init(ChatMessage)