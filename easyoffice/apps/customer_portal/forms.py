"""
apps/customer_portal/forms.py
==============================

Form classes used by the ticket-detail views. Keeping them here (instead
of inline in views.py) gives us:

  * Clean re-rendering on validation errors,
  * Centralised file-size / file-type rules,
  * A single place to add CAPTCHA/anti-abuse if it ever becomes needed.
"""
from __future__ import annotations

import os

from django import forms
from django.core.exceptions import ValidationError

# 10 MB hard cap per file, 5 files per submission.
MAX_FILE_SIZE = 10 * 1024 * 1024
MAX_FILES_PER_SUBMIT = 5

# Block obviously dangerous extensions even if MIME looks OK.
BLOCKED_EXTENSIONS = {
    '.exe', '.bat', '.cmd', '.com', '.scr', '.msi', '.pif',
    '.sh', '.ps1', '.vbs', '.jar', '.app', '.dmg',
    '.dll', '.so', '.deb', '.rpm',
}

ALLOWED_MIME_PREFIXES = (
    'image/',          # photos / screenshots
    'application/pdf',
    'text/plain',
    'text/csv',
    'application/msword',
    'application/vnd.openxmlformats-officedocument',
    'application/vnd.ms-excel',
    'application/zip',
    'application/x-zip-compressed',
    'video/mp4', 'video/quicktime',
)


def _validate_attachment(f) -> None:
    if f.size > MAX_FILE_SIZE:
        raise ValidationError(
            f'"{f.name}" is {f.size // (1024*1024)} MB — limit is '
            f'{MAX_FILE_SIZE // (1024*1024)} MB per file.'
        )

    ext = os.path.splitext(f.name)[1].lower()
    if ext in BLOCKED_EXTENSIONS:
        raise ValidationError(
            f'Files of type "{ext}" are not allowed for security reasons.'
        )

    ct = (getattr(f, 'content_type', '') or '').lower()
    if ct and not any(ct.startswith(p) for p in ALLOWED_MIME_PREFIXES):
        # Permissive fallback: don't block on MIME if extension looks safe;
        # the real defence is the extension blacklist above.
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Multi-file widget + field
#
# Django 5 removed the implicit `multiple=True` support from ClearableFileInput
# (constructing it with multiple=True now raises ValueError). The official
# replacement pattern is to subclass FileInput, opt allow_multiple_selected
# back in, and pair it with a FileField subclass that handles a *list* of
# files in to_python / validate.
# Reference: https://docs.djangoproject.com/en/5.0/topics/http/file-uploads/#uploading-multiple-files
# ─────────────────────────────────────────────────────────────────────────────

class MultiFileInput(forms.ClearableFileInput):
    """A FileInput that accepts <input type=file multiple>."""
    allow_multiple_selected = True


class MultiFileField(forms.FileField):
    """
    FileField variant that returns a list of UploadedFile objects instead of
    a single one. Validates each file through the parent's clean().
    """
    widget = MultiFileInput

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('widget', MultiFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        # `data` may be a single UploadedFile (one file picked) or a list
        # (multiple). Normalise to a list and clean each one.
        single_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_clean(d, initial) for d in data]
        # Empty / None: defer to parent — handles required vs not-required.
        if data in (None, ''):
            return single_clean(data, initial)
        return [single_clean(data, initial)]


# ─────────────────────────────────────────────────────────────────────────────
# Comment form (with optional attachments)
# ─────────────────────────────────────────────────────────────────────────────

class CommentForm(forms.Form):
    body = forms.CharField(
        widget=forms.Textarea(attrs={
            'class': 'p-textarea',
            'rows': 4,
            'placeholder': 'Add a reply, share a status update, or describe a new symptom…',
        }),
        max_length=8000,
        required=True,
        label='Your reply',
    )
    attachments = forms.FileField(
        required=False,
        widget=MultiFileInput(attrs={
            'class': 'p-input',
            'multiple': True,
            'accept': 'image/*,.pdf,.doc,.docx,.xls,.xlsx,.txt,.csv,.zip,video/*',
        }),
        label='Attach files (optional)',
    )

    def clean_body(self):
        body = (self.cleaned_data.get('body') or '').strip()
        if len(body) < 2:
            raise ValidationError('Please write at least a couple of words.')
        return body

    def clean(self):
        cleaned = super().clean()
        files = self.files.getlist('attachments') if self.files else []
        if len(files) > MAX_FILES_PER_SUBMIT:
            raise ValidationError(
                f'You can attach up to {MAX_FILES_PER_SUBMIT} files at once.'
            )
        for f in files:
            _validate_attachment(f)
        cleaned['attachment_files'] = files
        return cleaned


# ─────────────────────────────────────────────────────────────────────────────
# Cancel
# ─────────────────────────────────────────────────────────────────────────────

class CancelTicketForm(forms.Form):
    reason = forms.CharField(
        widget=forms.Textarea(attrs={
            'class': 'p-textarea',
            'rows': 3,
            'placeholder': 'Briefly tell us why you no longer need this — helps us improve.',
        }),
        max_length=2000,
        required=True,
        label='Reason for cancelling',
    )
    confirm = forms.BooleanField(
        required=True,
        label='I understand this cannot be undone — a new request will be needed if the issue returns.',
    )

    def clean_reason(self):
        reason = (self.cleaned_data.get('reason') or '').strip()
        if len(reason) < 5:
            raise ValidationError('Please give us a sentence or two so we can learn from it.')
        return reason


# ─────────────────────────────────────────────────────────────────────────────
# Reopen
# ─────────────────────────────────────────────────────────────────────────────

class ReopenForm(forms.Form):
    reason = forms.CharField(
        widget=forms.Textarea(attrs={
            'class': 'p-textarea',
            'rows': 4,
            'placeholder': 'What is still not working? When did it come back? Anything new since last time?',
        }),
        max_length=4000,
        required=True,
        label='Why should this ticket be reopened?',
    )

    def clean_reason(self):
        r = (self.cleaned_data.get('reason') or '').strip()
        if len(r) < 10:
            raise ValidationError(
                'Please describe what is still wrong — at least a sentence.'
            )
        return r


# ─────────────────────────────────────────────────────────────────────────────
# Rating
# ─────────────────────────────────────────────────────────────────────────────

class RatingForm(forms.Form):
    SCORE_CHOICES = [(str(i), str(i)) for i in range(1, 6)]

    score = forms.ChoiceField(
        choices=SCORE_CHOICES,
        widget=forms.HiddenInput,
        required=True,
    )
    feedback = forms.CharField(
        widget=forms.Textarea(attrs={
            'class': 'p-textarea',
            'rows': 3,
            'placeholder': 'Anything you want our team to know? (optional)',
        }),
        max_length=4000,
        required=False,
        label='Tell us more (optional)',
    )
    would_recommend = forms.TypedChoiceField(
        choices=(('', '—'), ('1', 'Yes'), ('0', 'No')),
        coerce=lambda v: True if v == '1' else (False if v == '0' else None),
        required=False,
        widget=forms.RadioSelect,
        label='Would you recommend us to others?',
    )

    def clean_score(self):
        v = self.cleaned_data.get('score')
        try:
            iv = int(v)
        except (TypeError, ValueError):
            raise ValidationError('Please pick a star rating.')
        if iv < 1 or iv > 5:
            raise ValidationError('Rating must be 1 to 5 stars.')
        return iv


# ─────────────────────────────────────────────────────────────────────────────
# Device verification
# ─────────────────────────────────────────────────────────────────────────────

class DeviceOTPForm(forms.Form):
    code = forms.CharField(
        max_length=6,
        min_length=6,
        widget=forms.TextInput(attrs={
            'class': 'p-input otp-input',
            'inputmode': 'numeric',
            'autocomplete': 'one-time-code',
            'pattern': r'\d{6}',
            'placeholder': '••••••',
            'maxlength': '6',
            'autofocus': 'autofocus',
        }),
        label='6-digit code',
    )

    def clean_code(self):
        code = (self.cleaned_data.get('code') or '').strip()
        if not code.isdigit() or len(code) != 6:
            raise ValidationError('Enter the 6-digit code from the email.')
        return code