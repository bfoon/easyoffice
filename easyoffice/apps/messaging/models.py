import uuid
from django.db import models
from django.utils.translation import gettext_lazy as _
from apps.core.models import User


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
    pinned_message = models.ForeignKey('ChatMessage', on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name='+')
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
        return self.messages.filter(is_deleted=False).first()


class ChatRoomMember(models.Model):
    class Role(models.TextChoices):
        ADMIN = 'admin', _('Admin')
        MEMBER = 'member', _('Member')
        READONLY = 'readonly', _('Read Only')

    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE)
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)
    last_read = models.DateTimeField(null=True, blank=True)
    is_muted = models.BooleanField(default=False)
    is_pinned = models.BooleanField(default=False)

    class Meta:
        unique_together = ['room', 'user']

    def __str__(self):
        return f'{self.user} in {self.room}'


class ChatMessage(models.Model):
    class MessageType(models.TextChoices):
        TEXT = 'text', _('Text')
        FILE = 'file', _('File')
        IMAGE = 'image', _('Image')
        SYSTEM = 'system', _('System')
        VOICE = 'voice', _('Voice Note')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_messages')
    message_type = models.CharField(max_length=20, choices=MessageType.choices, default=MessageType.TEXT)
    content = models.TextField(blank=True)
    file = models.FileField(upload_to='chat_files/%Y/%m/', null=True, blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    file_size = models.PositiveIntegerField(default=0)
    reply_to = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='replies')
    is_edited = models.BooleanField(default=False)
    edited_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    reads = models.ManyToManyField(User, related_name='read_messages', blank=True)
    reactions = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [models.Index(fields=['room', 'created_at'])]

    def __str__(self):
        return f'{self.sender}: {self.content[:50]}'
