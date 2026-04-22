from django.urls import path
from apps.core import views

urlpatterns = [
    # ── Authentication ────────────────────────────────────────────────────────
    path('login/',              views.LoginView.as_view(),                name='login'),
    path('logout/',             views.LogoutView.as_view(),               name='logout'),

    # ── OTP / device verification ─────────────────────────────────────────────
    path('otp/verify/',         views.OTPVerifyView.as_view(),            name='otp_verify'),
    path('otp/resend/',         views.ResendOTPView.as_view(),            name='otp_resend'),

    # ── Device switch (email one-click link) ──────────────────────────────────
    path('device/switch/<str:token>/', views.DeviceSwitchApproveView.as_view(), name='device_switch_approve'),
    path('device/switch/pending/',     views.SwitchPendingView.as_view(),        name='switch_pending'),

    # ── Trusted devices self-service ──────────────────────────────────────────
    path('devices/',            views.TrustedDevicesView.as_view(),       name='trusted_devices'),

    # ── Security dashboard ────────────────────────────────────────────────────
    path('security/',           views.SecurityDashboardView.as_view(),    name='security_dashboard'),

    # ── Profile & settings ────────────────────────────────────────────────────
    path('profile/',            views.ProfileView.as_view(),              name='profile'),
    path('profile/edit/',       views.ProfileEditView.as_view(),          name='profile_edit'),
    path('profile/password/',   views.ChangePasswordView.as_view(),       name='change_password'),
    path('settings/',           views.UserSettingsView.as_view(),         name='user_settings'),

    # ── Notifications ─────────────────────────────────────────────────────────
    path('notifications/',                  views.NotificationsView.as_view(),         name='notifications'),
    path('notifications/bell/',             views.NotificationsBellView.as_view(),     name='notifications_bell'),
    path('notifications/read/',             views.MarkNotificationsReadView.as_view(), name='mark_notifications_read'),
    path('notifications/read/<uuid:pk>/',   views.MarkNotificationReadView.as_view(),  name='mark_notification_read'),
]