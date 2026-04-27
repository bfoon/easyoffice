"""
apps.user_admin.urls
────────────────────
Two URL namespaces effectively:

   /users/...            admin console (protected by UserAdminRequiredMixin)
   /invite/<token>/      public — invitation acceptance
   /register/<token>/    public — self-registration via a RegistrationLink
   /change-password/     public-ish — forced password change after temp pw

Wire the whole thing into your project urls.py with a single include:

    path('users/', include('apps.user_admin.urls', namespace='user_admin')),
"""
from django.urls import path
from apps.user_admin import views

app_name = 'user_admin'

urlpatterns = [
    # ── Admin console ────────────────────────────────────────────────────
    path('',                          views.UserAdminDashboardView.as_view(),  name='dashboard'),
    path('create/',                   views.CreateUserView.as_view(),          name='create_user'),
    path('u/<uuid:pk>/',              views.UserDetailView.as_view(),          name='user_detail'),
    path('u/<uuid:pk>/action/',       views.UserActionView.as_view(),          name='user_action'),

    # Invitations
    path('invitations/',              views.InvitationListView.as_view(),      name='invitation_list'),
    path('invitations/<uuid:pk>/action/',
         views.InvitationActionView.as_view(),                                 name='invitation_action'),

    # Registration links
    path('links/',                    views.RegistrationLinkListView.as_view(),    name='registration_links'),
    path('links/create/',             views.RegistrationLinkCreateView.as_view(),  name='create_registration_link'),
    path('links/<uuid:pk>/action/',   views.RegistrationLinkActionView.as_view(),  name='registration_link_action'),

    # ── Public endpoints ────────────────────────────────────────────────
    # These are intentionally under the same urlconf for easy include, but
    # their URL *prefix* comes from wherever you mount this file. If you want
    # shorter paths (e.g. /invite/ instead of /users/invite/), wire these
    # explicitly in your project's root urls.py instead.
    path('invite/<str:token>/',       views.AcceptInvitationView.as_view(),    name='accept_invite'),
    path('register/<str:token>/',     views.SelfRegisterView.as_view(),        name='register_with_link'),
    path('change-password/',          views.ForcedPasswordChangeView.as_view(),name='force_change_password'),

    # ── Password reset (public — no login required) ───────────────────────
    path('password-reset/',
         views.PasswordResetRequestView.as_view(),
         name='password_reset_request'),

    path('reset/<str:uidb64>/<str:token>/',
         views.PasswordResetConfirmView.as_view(),
         name='password_reset_confirm'),

]
