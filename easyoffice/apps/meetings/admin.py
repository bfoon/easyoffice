from django.contrib import admin
from apps.meetings.models import Meeting, MeetingAttendee, MeetingMinutes, MeetingActionItem


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display  = ['title', 'meeting_type', 'organizer', 'start_datetime', 'status']
    list_filter   = ['meeting_type', 'status']
    search_fields = ['title', 'description']
    date_hierarchy = 'start_datetime'


@admin.register(MeetingAttendee)
class MeetingAttendeeAdmin(admin.ModelAdmin):
    list_display = ['meeting', 'user', 'rsvp', 'is_required']
    list_filter  = ['rsvp']


@admin.register(MeetingMinutes)
class MeetingMinutesAdmin(admin.ModelAdmin):
    list_display = ['meeting', 'author', 'is_final', 'created_at']


@admin.register(MeetingActionItem)
class MeetingActionItemAdmin(admin.ModelAdmin):
    list_display = ['description', 'minutes', 'assigned_to', 'due_date', 'is_done']
