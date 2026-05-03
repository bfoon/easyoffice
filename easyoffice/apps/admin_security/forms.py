"""
apps/admin_security/forms.py
"""
from django import forms

from .models import AdminAccessSetting


class AdminAccessSettingForm(forms.ModelForm):
    class Meta:
        model = AdminAccessSetting
        fields = ('is_admin_enabled', 'require_otp_for_admin', 'otp_validity_minutes')
        widgets = {
            'otp_validity_minutes': forms.NumberInput(attrs={'min': 1, 'max': 60}),
        }


class OTPChallengeForm(forms.Form):
    code = forms.CharField(
        label='6-digit code',
        min_length=6,
        max_length=6,
        widget=forms.TextInput(attrs={
            'autocomplete': 'one-time-code',
            'inputmode': 'numeric',
            'pattern': r'\d{6}',
            'autofocus': 'autofocus',
            'class': 'otp-input',
        }),
    )

    def clean_code(self):
        code = (self.cleaned_data.get('code') or '').strip()
        if not code.isdigit():
            raise forms.ValidationError('Enter the 6-digit code from your email.')
        return code
