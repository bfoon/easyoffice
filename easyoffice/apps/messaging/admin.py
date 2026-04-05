from django.contrib import admin
from apps.messaging.models import ChatRoom, ChatRoomMember, ChatMessage


class ChatRoomMemberInline(admin.TabularInline):
    model = ChatRoomMember
    extra = 0
    fields = ['user', 'role', 'joined_at', 'last_read', 'is_muted']
    readonly_fields = ['joined_at', 'last_read']
    autocomplete_fields = ['user']


@admin.register(ChatRoom)
class ChatRoomAdmin(admin.ModelAdmin):
    list_display = ['name', 'room_type', 'created_by', 'is_archived', 'is_readonly', 'created_at']
    list_filter = ['room_type', 'is_archived', 'is_readonly']
    search_fields = ['name', 'description']
    inlines = [ChatRoomMemberInline]
    readonly_fields = ['created_at', 'updated_at']
    fieldsets = (
        (None, {'fields': ('name', 'room_type', 'created_by', 'description', 'avatar')}),
        ('Linked entities', {'fields': ('unit', 'department', 'project'), 'classes': ('collapse',)}),
        ('Settings', {'fields': ('is_archived', 'is_readonly', 'pinned_message')}),
        ('Timestamps', {'fields': ('created_at', 'updated_at'), 'classes': ('collapse',)}),
    )


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ['sender', 'room', 'message_type', 'content_preview', 'is_deleted', 'created_at']
    list_filter = ['message_type', 'is_deleted', 'room__room_type']
    search_fields = ['content', 'sender__first_name', 'sender__last_name', 'room__name']
    readonly_fields = ['created_at', 'edited_at', 'deleted_at']
    date_hierarchy = 'created_at'

    def content_preview(self, obj):
        return obj.content[:60] if obj.content else f'[{obj.message_type}]'
    content_preview.short_description = 'Content'


@admin.register(ChatRoomMember)
class ChatRoomMemberAdmin(admin.ModelAdmin):
    list_display = ['user', 'room', 'role', 'joined_at', 'is_muted']
    list_filter = ['role', 'is_muted']
    search_fields = ['user__first_name', 'user__last_name', 'room__name']