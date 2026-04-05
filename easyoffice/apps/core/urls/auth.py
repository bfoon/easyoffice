from django.urls import path
from apps.core import views as auth_views

urlpatterns = [
    path('login/', auth_views.LoginView.as_view(), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('profile/', auth_views.ProfileView.as_view(), name='profile'),
    path('profile/edit/', auth_views.ProfileEditView.as_view(), name='profile_edit'),
    path('change-password/', auth_views.ChangePasswordView.as_view(), name='change_password'),
    path('settings/', auth_views.UserSettingsView.as_view(), name='user_settings'),
    path('notifications/', auth_views.NotificationsView.as_view(), name='notifications'),
    path('notifications/mark-read/', auth_views.MarkNotificationsReadView.as_view(), name='mark_notifications_read'),
]
