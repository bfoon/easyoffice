from django.contrib import admin
from apps.messaging.models import ChatRoom, ChatRoomMember, ChatMessage, ChatPoll, ChatPollOption, ChatPollVote


class ChatRoomMemberInline(admin.TabularInline):
    model = ChatRoomMember
    extra = 0
    fields = ['user', 'role', 'last_read']
    readonly_fields = ['last_read']
    autocomplete_fields = ['user']


@admin.register(ChatRoom)
class ChatRoomAdmin(admin.ModelAdmin):
    list_display = ['name', 'room_type', 'created_by', 'is_archived', 'is_readonly', 'created_at']
    list_filter = ['room_type', 'is_archived', 'is_readonly']
    search_fields = ['name', 'description']
    inlines = [ChatRoomMemberInline]
    readonly_fields = ['created_at', 'updated_at']
    fieldsets = (
        (None, {
            'fields': ('name', 'room_type', 'created_by', 'description', 'avatar')
        }),
        ('Linked entities', {
            'fields': ('unit', 'department', 'project'),
            'classes': ('collapse',)
        }),
        ('Settings', {
            'fields': ('is_archived', 'is_readonly', 'pinned_message')
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ['sender', 'room', 'message_type', 'content_preview', 'is_deleted', 'created_at']
    list_filter = ['message_type', 'is_deleted', 'room__room_type']
    search_fields = ['content', 'sender__first_name', 'sender__last_name', 'room__name']
    readonly_fields = ['created_at', 'edited_at']
    date_hierarchy = 'created_at'

    def content_preview(self, obj):
        return obj.content[:60] if obj.content else f'[{obj.message_type}]'
    content_preview.short_description = 'Content'


@admin.register(ChatRoomMember)
class ChatRoomMemberAdmin(admin.ModelAdmin):
    list_display = ['user', 'room', 'role', 'last_read']
    list_filter = ['role']
    search_fields = ['user__first_name', 'user__last_name', 'user__username', 'room__name']


class ChatPollOptionInline(admin.TabularInline):
    model = ChatPollOption
    extra = 0
    fields = ['text']
    show_change_link = True


@admin.register(ChatPoll)
class ChatPollAdmin(admin.ModelAdmin):
    list_display = [
        'question',
        'message',
        'created_by',
        'allow_multiple',
        'is_anonymous',
        'allow_vote_change',
        'is_closed',
        'ends_at',
        'created_at',
    ]
    list_filter = [
        'allow_multiple',
        'is_anonymous',
        'allow_vote_change',
        'is_closed',
        'created_at',
    ]
    search_fields = ['question', 'message__room__name', 'created_by__first_name', 'created_by__last_name']
    readonly_fields = ['created_at']
    inlines = [ChatPollOptionInline]


@admin.register(ChatPollOption)
class ChatPollOptionAdmin(admin.ModelAdmin):
    list_display = ['text', 'poll', 'vote_count']
    search_fields = ['text', 'poll__question']

    def vote_count(self, obj):
        return obj.votes.count()
    vote_count.short_description = 'Votes'


@admin.register(ChatPollVote)
class ChatPollVoteAdmin(admin.ModelAdmin):
    list_display = ['poll', 'option', 'user', 'voted_at']
    list_filter = ['voted_at']
    search_fields = ['poll__question', 'option__text', 'user__first_name', 'user__last_name', 'user__username']