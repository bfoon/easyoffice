from django.urls import path
from apps.core import views

urlpatterns = [
    path('login/',              views.LoginView.as_view(),                name='login'),
    path('logout/',             views.LogoutView.as_view(),               name='logout'),
    path('profile/',            views.ProfileView.as_view(),              name='profile'),
    path('profile/edit/',       views.ProfileEditView.as_view(),          name='profile_edit'),
    path('profile/password/',   views.ChangePasswordView.as_view(),       name='change_password'),
    path('settings/',           views.UserSettingsView.as_view(),         name='user_settings'),
    path('notifications/',      views.NotificationsView.as_view(),        name='notifications'),
    path('notifications/read/', views.MarkNotificationsReadView.as_view(),name='mark_notifications_read'),
]