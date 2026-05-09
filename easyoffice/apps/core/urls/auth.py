from django.urls import path
from apps.core.views import (
    LoginView,
    OTPVerifyView,
    ResendOTPView,
    ToggleOTPView,
    DeviceSwitchApproveView,
    SwitchPendingView,
    LogoutView,
    TrustedDevicesView,
    SecurityDashboardView,
    ProfileView,
    ProfileEditView,
    ChangePasswordView,
    UserSettingsView,
    NotificationsView,
    NotificationsBellView,
    MarkNotificationsReadView,
    MarkNotificationReadView,
)

urlpatterns = [
    path('login/', LoginView.as_view(), name='login'),
    path('otp/verify/', OTPVerifyView.as_view(), name='otp_verify'),
    path('otp/resend/', ResendOTPView.as_view(), name='resend_otp'),

    # Superuser-only: enable/disable OTP for any user.
    path('otp/toggle/<uuid:user_id>/', ToggleOTPView.as_view(), name='toggle_otp'),

    path('switch/pending/', SwitchPendingView.as_view(), name='switch_pending'),
    path('switch/approve/<str:token>/', DeviceSwitchApproveView.as_view(), name='device_switch_approve'),
    path('logout/', LogoutView.as_view(), name='logout'),

    path('trusted-devices/', TrustedDevicesView.as_view(), name='trusted_devices'),
    path('security/', SecurityDashboardView.as_view(), name='security_dashboard'),

    path('profile/', ProfileView.as_view(), name='profile'),
    path('profile/edit/', ProfileEditView.as_view(), name='profile_edit'),
    path('change-password/', ChangePasswordView.as_view(), name='change_password'),
    path('settings/', UserSettingsView.as_view(), name='user_settings'),

    # ── Notifications ─────────────────────────────────────────────────────────
    path('notifications/',                  NotificationsView.as_view(),         name='notifications'),
    path('notifications/bell/',             NotificationsBellView.as_view(),     name='notifications_bell'),
    path('notifications/read/',             MarkNotificationsReadView.as_view(), name='mark_notifications_read'),
    path('notifications/read/<uuid:pk>/',   MarkNotificationReadView.as_view(),  name='mark_notification_read'),
]