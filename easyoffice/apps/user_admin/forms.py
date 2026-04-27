"""Forms for user_admin. Kept small — most inputs are rendered directly in
templates, but validation for security-critical flows lives here."""

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.core.models import User
from apps.user_admin.models import AccountInvitation, RegistrationLink


class AcceptInvitationForm(forms.Form):
    """Form the new user fills out when accepting an email invitation."""
    first_name = forms.CharField(max_length=150)
    last_name  = forms.CharField(max_length=150)
    phone      = forms.CharField(max_length=30, required=False)
    password1  = forms.CharField(widget=forms.PasswordInput, label='Password')
    password2  = forms.CharField(widget=forms.PasswordInput, label='Confirm password')

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get('password1'), cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            raise ValidationError('Passwords do not match.')
        if p1:
            # Apply Django's configured AUTH_PASSWORD_VALIDATORS
            validate_password(p1)
        return cleaned


class SelfRegisterForm(forms.Form):
    """Self-registration form used via RegistrationLink."""
    email      = forms.EmailField()
    first_name = forms.CharField(max_length=150)
    last_name  = forms.CharField(max_length=150)
    phone      = forms.CharField(max_length=30, required=False)
    password1  = forms.CharField(widget=forms.PasswordInput, label='Password')
    password2  = forms.CharField(widget=forms.PasswordInput, label='Confirm password')

    def clean_email(self):
        email = (self.cleaned_data.get('email') or '').lower().strip()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError('An account with this email already exists. Please sign in instead.')
        return email

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get('password1'), cleaned.get('password2')
        if p1 and p2 and p1 != p2:
            raise ValidationError('Passwords do not match.')
        if p1:
            validate_password(p1)
        return cleaned


class ForcedPasswordChangeForm(forms.Form):
    """Used by the forced-change page after login with a temp password."""
    new_password1 = forms.CharField(widget=forms.PasswordInput, label='New password')
    new_password2 = forms.CharField(widget=forms.PasswordInput, label='Confirm new password')

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get('new_password1'), cleaned.get('new_password2')
        if p1 and p2 and p1 != p2:
            raise ValidationError('Passwords do not match.')
        if p1 and self.user:
            validate_password(p1, user=self.user)
        return cleaned


# ─────────────────────────────────────────────────────────────────────
# Append to existing forms.py — they reuse the same imports already there
# (django.forms, validate_password, ValidationError, User).
# ─────────────────────────────────────────────────────────────────────


class PasswordResetRequestForm(forms.Form):
    """
    'Forgot password' — user enters their email, we (maybe) email them a
    reset link. We deliberately do NOT validate that the email exists,
    so the form can't be used as a user-enumeration oracle. The view
    silently no-ops when the email doesn't match an account.
    """
    email = forms.EmailField()

    def clean_email(self):
        return (self.cleaned_data.get('email') or '').lower().strip()


class PasswordResetSetForm(forms.Form):
    """
    Form shown after clicking a valid reset link from an email — user
    chooses their new password. Mirrors ForcedPasswordChangeForm except
    `user` is supplied by the view from the decoded uidb64, not from the
    session.
    """
    new_password1 = forms.CharField(widget=forms.PasswordInput, label='New password')
    new_password2 = forms.CharField(widget=forms.PasswordInput, label='Confirm new password')

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        cleaned = super().clean()
        p1, p2 = cleaned.get('new_password1'), cleaned.get('new_password2')
        if p1 and p2 and p1 != p2:
            raise ValidationError('Passwords do not match.')
        if p1 and self.user:
            validate_password(p1, user=self.user)
        return cleaned