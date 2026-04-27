"""
apps.user_admin.views
─────────────────────
All administrative user-management endpoints + two public views
(invitation acceptance & self-registration).

Permission model:
  ─ Superusers AND members of the Django groups "CEO" or "Administration"
    may use the admin pages.
  ─ Two public endpoints (invite accept, self-register) are accessible to
    anonymous users — they're protected by token + usage limits instead.
"""

import secrets
import string
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.models import Group
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q, Count
from django.http import JsonResponse, HttpResponseForbidden, Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, View
from django.contrib.auth.tokens import default_token_generator
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode

from apps.core.models import User, AuditLog, CoreNotification
from apps.user_admin.models import (
    AccountInvitation, RegistrationLink, RegistrationLinkUsage, TempPassword,
)
from apps.user_admin.forms import (
    AcceptInvitationForm, SelfRegisterForm, ForcedPasswordChangeForm,
    PasswordResetRequestForm, PasswordResetSetForm,
)


# ─────────────────────────────────────────────────────────────────────────────
# Permission helpers
# ─────────────────────────────────────────────────────────────────────────────

ADMIN_GROUP_NAMES = ('CEO', 'Administration')


def is_user_admin(user):
    """Gatekeeper for the management console."""
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name__in=ADMIN_GROUP_NAMES).exists()


class UserAdminRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Reusable mixin — returns 403 for everyone who isn't an admin."""
    raise_exception = True  # show 403 instead of redirect-to-login loop

    def test_func(self):
        return is_user_admin(self.request.user)


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def _generate_temp_password(length=12):
    """
    Human-typable temp password: 12 chars, mix of upper/lower/digit.
    We deliberately avoid look-alike chars (0, O, l, 1, I) so the admin
    can read it out loud / copy from a sticky note without ambiguity.
    """
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789'
    # Guarantee at least one of each class so it passes typical validators.
    parts = [
        secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ'),
        secrets.choice('abcdefghijkmnpqrstuvwxyz'),
        secrets.choice('23456789'),
    ]
    parts += [secrets.choice(alphabet) for _ in range(length - 3)]
    # Shuffle without bias
    secrets.SystemRandom().shuffle(parts)
    return ''.join(parts)


def _audit(user, action, obj, notes='', changes=None, request=None):
    """Thin wrapper around AuditLog.objects.create so view code stays terse."""
    try:
        AuditLog.objects.create(
            user=user,
            action=action,
            model_name=obj.__class__.__name__,
            object_id=str(getattr(obj, 'pk', '') or ''),
            object_repr=str(obj)[:255],
            changes=changes,
            ip_address=_client_ip(request) if request else None,
            notes=notes[:3000] if notes else '',
        )
    except Exception:
        # Audit MUST never break the primary action.
        pass


def _client_ip(request):
    if not request:
        return None
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _apply_group_names(user, csv_names):
    """Add `user` to every group in `csv_names` (comma-separated). Silently
    skips groups that don't exist — they would have to be created by a
    superuser in Django admin first."""
    if not csv_names:
        return []
    wanted = [n.strip() for n in csv_names.split(',') if n.strip()]
    applied = []
    for name in wanted:
        g = Group.objects.filter(name=name).first()
        if g:
            user.groups.add(g)
            applied.append(name)
    return applied


def _build_absolute_url(request, path):
    """Best-effort absolute URL even when `request` may be None."""
    if request is not None:
        return request.build_absolute_uri(path)
    base = getattr(settings, 'SITE_BASE_URL', '').rstrip('/')
    return f'{base}{path}' if base else path


def _send_mail_safe(subject, body, to_email, request=None):
    """Wrap send_mail in try/except — email must never break flow."""
    try:
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@example.com')
        send_mail(subject, body, from_email, [to_email], fail_silently=True)
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Dashboard
# ─────────────────────────────────────────────────────────────────────────────

class UserAdminDashboardView(UserAdminRequiredMixin, TemplateView):
    template_name = 'user_admin/dashboard.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q          = (self.request.GET.get('q') or '').strip()
        status_f   = (self.request.GET.get('status') or '').strip()
        group_f    = (self.request.GET.get('group') or '').strip()
        locked_f   = self.request.GET.get('locked') == '1'

        users_qs = User.objects.all().prefetch_related('groups').order_by('last_name', 'first_name')

        if q:
            users_qs = users_qs.filter(
                Q(first_name__icontains=q) | Q(last_name__icontains=q) |
                Q(email__icontains=q) | Q(username__icontains=q) |
                Q(employee_id__icontains=q)
            )
        if status_f:
            users_qs = users_qs.filter(status=status_f)
        if group_f:
            users_qs = users_qs.filter(groups__name=group_f)
        if locked_f:
            users_qs = users_qs.filter(lockout_until__gt=timezone.now())

        users_qs = users_qs.distinct()

        # Stats
        all_users = User.objects.all()
        stats = {
            'total':      all_users.count(),
            'active':     all_users.filter(status=User.Status.ACTIVE, is_active=True).count(),
            'suspended':  all_users.filter(status=User.Status.SUSPENDED).count(),
            'on_leave':   all_users.filter(status=User.Status.ON_LEAVE).count(),
            'terminated': all_users.filter(status=User.Status.TERMINATED).count(),
            'locked':     all_users.filter(lockout_until__gt=timezone.now()).count(),
            'pending_invites': AccountInvitation.objects
                                .filter(status=AccountInvitation.Status.PENDING,
                                        expires_at__gt=timezone.now()).count(),
            'active_links':    RegistrationLink.objects
                                .filter(status=RegistrationLink.Status.ACTIVE,
                                        expires_at__gt=timezone.now()).count(),
        }

        ctx.update({
            'users': users_qs[:500],  # cap for dashboard render
            'total_matches': users_qs.count(),
            'stats': stats,
            'q': q,
            'status_f': status_f,
            'group_f': group_f,
            'locked_f': locked_f,
            'all_statuses': User.Status.choices,
            'all_groups': Group.objects.order_by('name'),
        })
        return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Admin: User detail + actions
# ─────────────────────────────────────────────────────────────────────────────

class UserDetailView(UserAdminRequiredMixin, TemplateView):
    template_name = 'user_admin/user_detail.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = get_object_or_404(User, pk=kwargs['pk'])
        ctx['target'] = user
        ctx['is_locked'] = user.is_locked_out()
        ctx['must_change_pw'] = TempPassword.user_must_change_password(user)
        ctx['recent_events'] = user.security_events.all()[:20] if hasattr(user, 'security_events') else []
        ctx['audit_logs'] = AuditLog.objects.filter(
            object_id=str(user.pk), model_name='User'
        ).order_by('-timestamp')[:20]
        ctx['all_groups'] = Group.objects.order_by('name')
        ctx['user_groups'] = list(user.groups.values_list('name', flat=True))
        return ctx


class CreateUserView(UserAdminRequiredMixin, View):
    """
    Two modes (POST body `mode`):
      mode=invite      → create an AccountInvitation, email the link
      mode=temp_pw     → create the User now + issue a temp password
    GET renders the form.
    """
    template_name = 'user_admin/create_user.html'

    def get(self, request):
        return render(request, self.template_name, {
            'all_groups': Group.objects.order_by('name'),
        })

    def post(self, request):
        mode = (request.POST.get('mode') or 'invite').strip()
        email = (request.POST.get('email') or '').strip().lower()
        first_name = (request.POST.get('first_name') or '').strip()
        last_name = (request.POST.get('last_name') or '').strip()
        employee_id = (request.POST.get('employee_id') or '').strip()
        # group_names = (request.POST.get('group_names') or '').strip()
        personal_msg = (request.POST.get('message') or '').strip()
        expires_days = max(1, min(30, int(request.POST.get('expires_days') or 7)))

        raw_group_list = [g.strip() for g in request.POST.getlist('group_names') if g.strip()]
        if not raw_group_list:
            legacy_group_value = (request.POST.get('group_names') or '').strip()
            if legacy_group_value:
                raw_group_list = [g.strip() for g in legacy_group_value.split(',') if g.strip()]
        group_names = ', '.join(dict.fromkeys(raw_group_list))

        if not email:
            messages.error(request, 'Email is required.')
            return redirect('user_admin:create_user')

        if User.objects.filter(email__iexact=email).exists():
            messages.error(request, f'A user with email {email} already exists.')
            return redirect('user_admin:create_user')

        # ── MODE 1: Email invitation ────────────────────────────────────
        if mode == 'invite':
            if AccountInvitation.objects.filter(
                email__iexact=email, status=AccountInvitation.Status.PENDING,
                expires_at__gt=timezone.now(),
            ).exists():
                messages.warning(request, f'A pending invitation already exists for {email}. Revoke it first or resend.')
                return redirect('user_admin:invitation_list')

            invite = AccountInvitation.objects.create(
                email=email,
                first_name=first_name,
                last_name=last_name,
                employee_id=employee_id,
                group_names=group_names,
                message=personal_msg,
                invited_by=request.user,
                expires_at=timezone.now() + timedelta(days=expires_days),
            )
            _send_invitation_email(invite, request)
            _audit(request.user, AuditLog.Action.CREATE, invite,
                   notes=f'Invited {email}', request=request)
            messages.success(request, f'Invitation sent to {email}. They have {expires_days} day(s) to accept.')
            return redirect('user_admin:invitation_list')

        # ── MODE 2: Create now + temp password ──────────────────────────
        if mode == 'temp_pw':
            username = email  # USERNAME_FIELD is email, but Django still needs a username col
            temp_pw = _generate_temp_password()

            with transaction.atomic():
                user = User.objects.create(
                    email=email,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    employee_id=employee_id or None,
                    status=User.Status.ACTIVE,
                    is_active=True,
                )
                user.set_password(temp_pw)
                user.save()

                applied_groups = _apply_group_names(user, group_names)

                TempPassword.objects.create(
                    user=user,
                    issued_by=request.user,
                    password_hint=temp_pw[-4:],
                )

            _audit(request.user, AuditLog.Action.CREATE, user,
                   notes=f'Created user {email} with temp password. Groups: {applied_groups}',
                   request=request)

            # Store the plaintext temp pw in a one-shot flash so the detail
            # page can show it ONCE and then forget about it.
            request.session['temp_pw_flash'] = {
                'user_id': str(user.pk),
                'email': email,
                'password': temp_pw,
            }
            messages.success(request, f'Created {email}. Show them the temporary password now — it will not be displayed again.')
            return redirect('user_admin:user_detail', pk=user.pk)

        messages.error(request, 'Unknown onboarding mode.')
        return redirect('user_admin:create_user')


class UserActionView(UserAdminRequiredMixin, View):
    """
    Single endpoint for all per-user actions. POST body `action` selects:
      reset_email   — send password-reset email
      reset_temp    — generate new temp password (displayed once)
      block         — status=SUSPENDED, is_active=False
      unblock       — status=ACTIVE, is_active=True
      terminate     — status=TERMINATED, is_active=False
      unlock        — clear failed_login_count + lockout_until
      change_status — status=<request.POST['status']>
      set_groups    — replace groups with request.POST.getlist('groups')
      delete        — hard-delete the user (superuser only)
    """

    # ACTION_METHODS registers the allowed actions + their handler methods.
    def post(self, request, pk):
        target = get_object_or_404(User, pk=pk)
        action = (request.POST.get('action') or '').strip()

        if target.id == request.user.id and action in ('block', 'terminate', 'delete'):
            messages.error(request, "You cannot perform that action on your own account.")
            return redirect('user_admin:user_detail', pk=pk)

        handler = getattr(self, f'_do_{action}', None)
        if not handler:
            messages.error(request, f'Unknown action: {action}')
            return redirect('user_admin:user_detail', pk=pk)

        handler(request, target)
        return redirect('user_admin:user_detail', pk=pk)

    # ── Action handlers ───────────────────────────────────────────────────

    def _do_reset_email(self, request, target):
        """Send a password-reset link from the admin console."""
        try:
            url = _build_password_reset_url(request, target)
            body = (
                f'Hello {getattr(target, "full_name", None) or target.email},\n\n'
                f'A password reset has been requested for your account by an administrator.\n'
                f'Open this link to set a new password '
                f'(the link expires shortly and can only be used once):\n\n'
                f'{url}\n\n'
                f'If you did not expect this email, ignore it — your password remains unchanged.\n'
            )
            _send_mail_safe('Password reset', body, target.email, request=request)
            _audit(request.user, AuditLog.Action.UPDATE, target,
                   notes='Sent password-reset email', request=request)
            messages.success(request, f'Password-reset email sent to {target.email}.')
        except Exception as e:
            messages.error(request, f'Could not send reset email: {e}')

    def _do_reset_temp(self, request, target):
        temp_pw = _generate_temp_password()
        target.set_password(temp_pw)
        target.save(update_fields=['password'])
        TempPassword.objects.create(
            user=target, issued_by=request.user, password_hint=temp_pw[-4:],
        )
        _audit(request.user, AuditLog.Action.UPDATE, target,
               notes='Reset password with temp (force-change on next login)', request=request)
        request.session['temp_pw_flash'] = {
            'user_id': str(target.pk),
            'email': target.email,
            'password': temp_pw,
        }
        messages.success(request, 'Temporary password generated. Show it to the user now.')

    def _do_block(self, request, target):
        target.status = User.Status.SUSPENDED
        target.is_active = False
        target.save(update_fields=['status', 'is_active'])
        _audit(request.user, AuditLog.Action.UPDATE, target,
               notes='Blocked (suspended)', request=request)
        messages.success(request, f'{target.email} blocked.')

    def _do_unblock(self, request, target):
        target.status = User.Status.ACTIVE
        target.is_active = True
        target.save(update_fields=['status', 'is_active'])
        _audit(request.user, AuditLog.Action.UPDATE, target,
               notes='Unblocked', request=request)
        messages.success(request, f'{target.email} unblocked.')

    def _do_terminate(self, request, target):
        target.status = User.Status.TERMINATED
        target.is_active = False
        target.save(update_fields=['status', 'is_active'])
        _audit(request.user, AuditLog.Action.UPDATE, target,
               notes='Terminated', request=request)
        messages.success(request, f'{target.email} marked terminated.')

    def _do_unlock(self, request, target):
        target.clear_failed_logins()
        _audit(request.user, AuditLog.Action.UPDATE, target,
               notes='Cleared failed-login lockout', request=request)
        messages.success(request, f'{target.email} unlocked.')

    def _do_change_status(self, request, target):
        new_status = request.POST.get('status')
        valid = {s[0] for s in User.Status.choices}
        if new_status not in valid:
            messages.error(request, 'Invalid status.')
            return
        target.status = new_status
        # Align is_active with business rules
        target.is_active = new_status in (User.Status.ACTIVE, User.Status.ON_LEAVE)
        target.save(update_fields=['status', 'is_active'])
        _audit(request.user, AuditLog.Action.UPDATE, target,
               notes=f'Status changed to {new_status}', request=request)
        messages.success(request, f'Status updated.')

    def _do_set_groups(self, request, target):
        names = request.POST.getlist('groups')
        groups = Group.objects.filter(name__in=names)
        target.groups.set(groups)
        _audit(request.user, AuditLog.Action.UPDATE, target,
               notes=f'Groups set to: {list(groups.values_list("name", flat=True))}',
               request=request)
        messages.success(request, 'Groups updated.')

    def _do_delete(self, request, target):
        if not request.user.is_superuser:
            messages.error(request, 'Only superusers may permanently delete accounts.')
            return
        email = target.email
        _audit(request.user, AuditLog.Action.DELETE, target,
               notes=f'Hard-deleted {email}', request=request)
        target.delete()
        messages.success(request, f'{email} permanently deleted.')


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Invitations
# ─────────────────────────────────────────────────────────────────────────────

class InvitationListView(UserAdminRequiredMixin, TemplateView):
    template_name = 'user_admin/invitation_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Auto-expire stale pending invitations on view
        AccountInvitation.objects.filter(
            status=AccountInvitation.Status.PENDING,
            expires_at__lte=timezone.now(),
        ).update(status=AccountInvitation.Status.EXPIRED)

        invitations = AccountInvitation.objects.select_related('invited_by', 'accepted_by').order_by('-created_at')
        ctx['invitations'] = invitations[:500]
        return ctx


class InvitationActionView(UserAdminRequiredMixin, View):
    """actions: resend / revoke"""

    def post(self, request, pk):
        invite = get_object_or_404(AccountInvitation, pk=pk)
        action = request.POST.get('action')

        if action == 'resend':
            if invite.status != AccountInvitation.Status.PENDING:
                # Re-activate for another cycle
                invite.status = AccountInvitation.Status.PENDING
                invite.expires_at = timezone.now() + timedelta(days=AccountInvitation.DEFAULT_EXPIRY_DAYS)
                invite.save(update_fields=['status', 'expires_at'])
            _send_invitation_email(invite, request)
            _audit(request.user, AuditLog.Action.UPDATE, invite, notes='Resent', request=request)
            messages.success(request, f'Invitation re-sent to {invite.email}.')
        elif action == 'revoke':
            invite.status = AccountInvitation.Status.REVOKED
            invite.save(update_fields=['status'])
            _audit(request.user, AuditLog.Action.UPDATE, invite, notes='Revoked', request=request)
            messages.success(request, f'Invitation for {invite.email} revoked.')
        else:
            messages.error(request, 'Unknown invitation action.')

        return redirect('user_admin:invitation_list')


def _send_invitation_email(invite, request):
    """Compose and send the acceptance link."""
    path = reverse('user_admin:accept_invite', kwargs={'token': invite.token})
    url = _build_absolute_url(request, path)
    greeting = invite.first_name or 'there'
    body = (
        f'Hi {greeting},\n\n'
        f'{invite.invited_by.full_name if invite.invited_by else "An administrator"} '
        f'has invited you to join the team.\n\n'
    )
    if invite.message:
        body += f'Personal note:\n"{invite.message}"\n\n'
    body += (
        f'To accept and set your password, open this link before '
        f'{invite.expires_at:%b %d, %Y at %H:%M %Z}:\n\n'
        f'{url}\n\n'
        f'This link can only be used once.'
    )
    _send_mail_safe('You are invited to join', body, invite.email, request=request)


# ─────────────────────────────────────────────────────────────────────────────
# Admin: Registration links
# ─────────────────────────────────────────────────────────────────────────────

class RegistrationLinkListView(UserAdminRequiredMixin, TemplateView):
    template_name = 'user_admin/registration_link_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Refresh stale
        for l in RegistrationLink.objects.filter(status=RegistrationLink.Status.ACTIVE):
            l.refresh_status()

        links = RegistrationLink.objects.select_related('created_by').order_by('-created_at')
        # Build absolute URL per link for easy copy-paste
        result = []
        for l in links[:500]:
            path = reverse('user_admin:register_with_link', kwargs={'token': l.token})
            result.append({
                'obj': l,
                'url': _build_absolute_url(self.request, path),
            })
        ctx['links'] = result
        return ctx


class RegistrationLinkCreateView(UserAdminRequiredMixin, View):
    def post(self, request):
        label = (request.POST.get('label') or '').strip() or 'Registration link'
        group_names = (request.POST.get('group_names') or '').strip()
        try:
            max_uses = max(1, min(100, int(request.POST.get('max_uses') or 5)))
        except ValueError:
            max_uses = 5
        try:
            expires_days = max(1, min(60, int(request.POST.get('expires_days') or 7)))
        except ValueError:
            expires_days = 7

        link = RegistrationLink.objects.create(
            label=label,
            group_names=group_names,
            max_uses=max_uses,
            expires_at=timezone.now() + timedelta(days=expires_days),
            created_by=request.user,
        )
        _audit(request.user, AuditLog.Action.CREATE, link,
               notes=f'Created registration link "{label}" (max_uses={max_uses}, {expires_days}d)',
               request=request)
        messages.success(request, f'Registration link created. Share it with up to {max_uses} people.')
        return redirect('user_admin:registration_links')


class RegistrationLinkActionView(UserAdminRequiredMixin, View):
    def post(self, request, pk):
        link = get_object_or_404(RegistrationLink, pk=pk)
        action = request.POST.get('action')
        if action == 'revoke':
            link.status = RegistrationLink.Status.REVOKED
            link.save(update_fields=['status'])
            _audit(request.user, AuditLog.Action.UPDATE, link, notes='Revoked', request=request)
            messages.success(request, 'Link revoked.')
        elif action == 'extend':
            try:
                days = max(1, min(60, int(request.POST.get('days') or 7)))
            except ValueError:
                days = 7
            link.expires_at = timezone.now() + timedelta(days=days)
            if link.status != RegistrationLink.Status.EXHAUSTED:
                link.status = RegistrationLink.Status.ACTIVE
            link.save(update_fields=['expires_at', 'status'])
            _audit(request.user, AuditLog.Action.UPDATE, link,
                   notes=f'Extended by {days}d', request=request)
            messages.success(request, f'Link extended by {days} day(s).')
        return redirect('user_admin:registration_links')


# ─────────────────────────────────────────────────────────────────────────────
# Public: Accept invitation (anyone with the token)
# ─────────────────────────────────────────────────────────────────────────────

class AcceptInvitationView(View):
    template_name = 'user_admin/accept_invite.html'

    def _fetch(self, token):
        invite = AccountInvitation.objects.filter(token=token).first()
        if not invite:
            raise Http404('Invitation not found.')
        invite.mark_expired_if_needed()
        return invite

    def get(self, request, token):
        invite = self._fetch(token)
        form = AcceptInvitationForm(initial={
            'first_name': invite.first_name,
            'last_name': invite.last_name,
        })
        return render(request, self.template_name, {'invite': invite, 'form': form})

    def post(self, request, token):
        invite = self._fetch(token)
        if invite.status != AccountInvitation.Status.PENDING:
            messages.error(request, f'This invitation is {invite.status}.')
            return redirect('login')

        # Someone else may have claimed the email in the meantime
        if User.objects.filter(email__iexact=invite.email).exists():
            invite.status = AccountInvitation.Status.REVOKED
            invite.save(update_fields=['status'])
            messages.error(request, 'This email is already registered. Please sign in instead.')
            return redirect('login')

        form = AcceptInvitationForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {'invite': invite, 'form': form})

        with transaction.atomic():
            user = User.objects.create(
                email=invite.email,
                username=invite.email,
                first_name=form.cleaned_data['first_name'],
                last_name=form.cleaned_data['last_name'],
                phone=form.cleaned_data.get('phone', ''),
                employee_id=invite.employee_id or None,
                status=User.Status.ACTIVE,
                is_active=True,
            )
            user.set_password(form.cleaned_data['password1'])
            user.save()
            _apply_group_names(user, invite.group_names)

            invite.status = AccountInvitation.Status.ACCEPTED
            invite.accepted_by = user
            invite.accepted_at = timezone.now()
            invite.save(update_fields=['status', 'accepted_by', 'accepted_at'])

        _audit(invite.invited_by, AuditLog.Action.CREATE, user,
               notes=f'Account created by invitation acceptance', request=request)

        # Auto-login for a smooth onboarding
        login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        messages.success(request, f'Welcome, {user.first_name}!')
        return redirect('/')  # send to site home; adjust if you have a dashboard URL


# ─────────────────────────────────────────────────────────────────────────────
# Public: Self-register via RegistrationLink
# ─────────────────────────────────────────────────────────────────────────────

class SelfRegisterView(View):
    template_name = 'user_admin/self_register.html'

    def _fetch(self, token):
        link = RegistrationLink.objects.filter(token=token).first()
        if not link:
            raise Http404('Registration link not found.')
        link.refresh_status()
        return link

    def get(self, request, token):
        link = self._fetch(token)
        form = SelfRegisterForm()
        return render(request, self.template_name, {'link': link, 'form': form})

    def post(self, request, token):
        link = self._fetch(token)
        if not link.is_usable:
            messages.error(request, 'This registration link is no longer active.')
            return render(request, self.template_name, {'link': link, 'form': SelfRegisterForm()})

        form = SelfRegisterForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {'link': link, 'form': form})

        with transaction.atomic():
            # Re-check under lock — cheap guard against racing signups
            link.refresh_from_db()
            if not link.is_usable:
                messages.error(request, 'This registration link just became unavailable.')
                return render(request, self.template_name, {'link': link, 'form': SelfRegisterForm()})

            user = User.objects.create(
                email=form.cleaned_data['email'],
                username=form.cleaned_data['email'],
                first_name=form.cleaned_data['first_name'],
                last_name=form.cleaned_data['last_name'],
                phone=form.cleaned_data.get('phone', ''),
                status=User.Status.ACTIVE,
                is_active=True,
            )
            user.set_password(form.cleaned_data['password1'])
            user.save()
            _apply_group_names(user, link.group_names)

            link.use_count += 1
            link.save(update_fields=['use_count'])
            link.refresh_status()
            RegistrationLinkUsage.objects.create(link=link, user=user)

        _audit(link.created_by, AuditLog.Action.CREATE, user,
               notes=f'Self-registered via link "{link.label}"', request=request)

        login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        messages.success(request, f'Welcome, {user.first_name}!')
        return redirect('/')


# ─────────────────────────────────────────────────────────────────────────────
# Forced password change (after temp-password login)
# ─────────────────────────────────────────────────────────────────────────────

class ForcedPasswordChangeView(LoginRequiredMixin, View):
    template_name = 'user_admin/force_change_password.html'

    def get(self, request):
        # If they don't actually need to change, bounce them back.
        if not TempPassword.user_must_change_password(request.user):
            return redirect('/')
        form = ForcedPasswordChangeForm(user=request.user)
        return render(request, self.template_name, {'form': form})

    def post(self, request):
        if not TempPassword.user_must_change_password(request.user):
            return redirect('/')

        form = ForcedPasswordChangeForm(request.POST, user=request.user)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form})

        request.user.set_password(form.cleaned_data['new_password1'])
        request.user.save(update_fields=['password'])
        TempPassword.mark_user_pw_changed(request.user)

        # Keep the user logged-in with the new password
        update_session_auth_hash(request, request.user)

        _audit(request.user, AuditLog.Action.UPDATE, request.user,
               notes='User changed their temp password', request=request)

        messages.success(request, 'Password updated.')
        return redirect('/')


# ─────────────────────────────────────────────────────────────────────────────
# Public: Password reset by email
#
# Two views, mirroring Django's stock flow but living entirely inside
# user_admin so we can:
#   - reuse our _audit / _send_mail_safe helpers
#   - consume any outstanding TempPassword on successful reset
#   - keep templates in user_admin/ alongside force_change_password.html
# ─────────────────────────────────────────────────────────────────────────────


def _build_password_reset_url(request, user):
    """
    Returns the absolute URL the user should click in their email.
    Centralised so both _do_reset_email (admin-initiated) and
    PasswordResetRequestView (user-initiated) generate the same URL.
    """
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.http import urlsafe_base64_encode
    from django.utils.encoding import force_bytes

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    path = reverse('user_admin:password_reset_confirm',
                   kwargs={'uidb64': uid, 'token': token})
    return _build_absolute_url(request, path)


class PasswordResetRequestView(View):
    """
    'Forgot your password?' — anonymous user enters their email; if it
    matches an account, we send a reset link. We always show the same
    confirmation page regardless of whether the email matched (no
    enumeration leaks).
    """
    template_name = 'user_admin/password_reset_request.html'
    done_template = 'user_admin/password_reset_done.html'

    def get(self, request):
        return render(request, self.template_name,
                      {'form': PasswordResetRequestForm()})

    def post(self, request):
        form = PasswordResetRequestForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {'form': form})

        email = form.cleaned_data['email']
        # Look up case-insensitively. We don't reveal whether a match was
        # found — the response page is identical either way.
        user = (
            User.objects
            .filter(email__iexact=email, is_active=True)
            .first()
        )
        if user:
            try:
                url = _build_password_reset_url(request, user)
                body = (
                    f'Hello {getattr(user, "full_name", None) or user.email},\n\n'
                    f'We received a request to reset the password on your account.\n'
                    f'Click the link below to choose a new password '
                    f'(the link expires shortly and can only be used once):\n\n'
                    f'{url}\n\n'
                    f'If you did not request this, you can safely ignore this email — '
                    f'your password will stay the same.\n'
                )
                _send_mail_safe('Reset your password', body, user.email, request=request)
                _audit(user, AuditLog.Action.UPDATE, user,
                       notes='User requested password-reset email', request=request)
            except Exception:
                # Never break the user-facing flow on email errors.
                pass

        return render(request, self.done_template, {'email': email})


class PasswordResetConfirmView(View):
    """
    The link from the reset email lands here. We decode `uidb64` to find
    the user, validate `token`, then either show the 'set new password'
    form or an 'invalid link' page.

    On successful change:
      - update_session_auth_hash isn't needed (user is anonymous here)
      - any outstanding TempPassword for this user is marked consumed
      - audit log gets a row
      - user is redirected to login (NOT auto-logged-in, deliberately —
        forces a fresh sign-in with the new credentials and re-engages
        OTP / device-trust if those exist)
    """
    template_name = 'user_admin/password_reset_confirm.html'
    invalid_template = 'user_admin/password_reset_invalid.html'

    def _resolve_user(self, uidb64, token):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.http import urlsafe_base64_decode
        try:
            uid = urlsafe_base64_decode(uidb64).decode()
            user = User.objects.get(pk=uid)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return None
        if not default_token_generator.check_token(user, token):
            return None
        if not user.is_active:
            return None
        return user

    def get(self, request, uidb64, token):
        user = self._resolve_user(uidb64, token)
        if not user:
            return render(request, self.invalid_template, status=400)
        return render(request, self.template_name, {
            'form': PasswordResetSetForm(user=user),
            'validlink': True,
        })

    def post(self, request, uidb64, token):
        user = self._resolve_user(uidb64, token)
        if not user:
            return render(request, self.invalid_template, status=400)

        form = PasswordResetSetForm(request.POST, user=user)
        if not form.is_valid():
            return render(request, self.template_name, {
                'form': form, 'validlink': True,
            })

        with transaction.atomic():
            user.set_password(form.cleaned_data['new_password1'])
            user.save(update_fields=['password'])
            # If they had a temp password outstanding, this satisfies it
            # — no double-prompt to change again on next login.
            TempPassword.mark_user_pw_changed(user)

        _audit(user, AuditLog.Action.UPDATE, user,
               notes='Password reset via email link', request=request)

        messages.success(
            request,
            'Your password has been updated. Please sign in with your new password.',
        )
        # Send to login. We deliberately do NOT auto-login so that any
        # OTP / device-trust flow on the login view runs as normal.
        login_url = reverse('login') if _has_url('login') else '/login/'
        return redirect(login_url)


def _has_url(name):
    """Cheap check for whether a URL name resolves; defined locally so the
    middleware-style `_safe_reverse` doesn't have to be imported."""
    from django.urls import NoReverseMatch
    try:
        reverse(name)
        return True
    except NoReverseMatch:
        return False