from django.urls import path
from apps.meetings import views

urlpatterns = [
    path('',                              views.MeetingListView.as_view(),        name='meeting_list'),
    path('create/',                       views.MeetingCreateView.as_view(),      name='meeting_create'),
    path('<uuid:pk>/',                    views.MeetingDetailView.as_view(),      name='meeting_detail'),
    path('<uuid:pk>/edit/',               views.MeetingUpdateView.as_view(),      name='meeting_edit'),
    path('<uuid:pk>/cancel/',             views.MeetingCancelView.as_view(),      name='meeting_cancel'),
    path('<uuid:pk>/rsvp/',               views.MeetingRSVPView.as_view(),        name='meeting_rsvp'),
    path('<uuid:pk>/minutes/',            views.MeetingMinutesView.as_view(),     name='meeting_minutes'),
    path('<uuid:pk>/minutes/pdf/',        views.MeetingMinutesPDFView.as_view(),  name='meeting_minutes_pdf'),
]
