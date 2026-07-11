from django.urls import path
from apps.meetings import views, ai_views

urlpatterns = [
    path('',                                  views.MeetingListView.as_view(),                    name='meeting_list'),
    path('create/',                           views.MeetingCreateView.as_view(),                  name='meeting_create'),
    path('minutes/review/<str:token>/',       views.MeetingMinutesExternalReviewView.as_view(),   name='meeting_minutes_external_review'),

    # ── Calendar ─────────────────────────────────────────────────────────────
    path('calendar/feed/',                    ai_views.MeetingCalendarFeedView.as_view(),         name='meeting_calendar_feed'),

    path('<uuid:pk>/',                        views.MeetingDetailView.as_view(),                  name='meeting_detail'),
    path('<uuid:pk>/edit/',                   views.MeetingUpdateView.as_view(),                  name='meeting_edit'),
    path('<uuid:pk>/end/',                    views.MeetingEndView.as_view(),                     name='meeting_end'),
    path('<uuid:pk>/cancel/',                 views.MeetingCancelView.as_view(),                  name='meeting_cancel'),
    path('<uuid:pk>/rsvp/',                   views.MeetingRSVPView.as_view(),                    name='meeting_rsvp'),
    path('<uuid:pk>/followup/',               views.MeetingFollowUpView.as_view(),                name='meeting_followup'),
    path('<uuid:pk>/minutes/',                views.MeetingMinutesView.as_view(),                 name='meeting_minutes'),
    path('<uuid:pk>/minutes/pdf/',            views.MeetingMinutesPDFView.as_view(),              name='meeting_minutes_pdf'),
    path('<uuid:pk>/minutes/share/',          views.ShareMeetingMinutesView.as_view(),            name='meeting_minutes_share'),
    path('<uuid:pk>/attendance/',             views.MeetingAttendanceUpdateView.as_view(),        name='meeting_attendance_update'),

    # ── Recording & AI transcription ─────────────────────────────────────────
    path('<uuid:pk>/recordings/',             ai_views.MeetingRecordingView.as_view(),            name='meeting_recordings'),
    path('<uuid:pk>/recordings/<uuid:rid>/',  ai_views.MeetingRecordingManageView.as_view(),      name='meeting_recording_manage'),

    # ── Minutes helpers ──────────────────────────────────────────────────────
    path('<uuid:pk>/minutes/autosave/',       ai_views.MinutesAutosaveView.as_view(),             name='meeting_minutes_autosave'),
    path('<uuid:pk>/minutes/ai-draft/',       ai_views.MinutesAIDraftView.as_view(),              name='meeting_minutes_ai_draft'),
]