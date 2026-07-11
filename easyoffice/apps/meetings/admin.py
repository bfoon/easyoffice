from django.contrib import admin
from apps.meetings.models import (
    Meeting, MeetingAttendee, MeetingMinutes, MeetingActionItem,
    MeetingRecording,
)


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


@admin.register(MeetingRecording)
class MeetingRecordingAdmin(admin.ModelAdmin):
    list_display  = ['display_title', 'meeting', 'recorded_by', 'duration_display',
                     'transcript_status', 'created_at']
    list_filter   = ['transcript_status']
    search_fields = ['title', 'meeting__title', 'transcript']
    readonly_fields = ['transcript', 'transcript_error', 'transcribed_at']