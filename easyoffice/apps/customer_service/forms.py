"""
apps/customer_service/forms.py
──────────────────────────────
Forms used by the customer-service module.

Currently only houses the standalone "create contact" form (used outside
the call desk). The call-desk flow keeps its own inline POST handling
inside views.py / views_extra.py.
"""
from django import forms
from django.forms import inlineformset_factory

from .models import Customer, CustomerPhone


class CustomerForm(forms.ModelForm):
    """Create / edit a Customer record from the standalone contact page."""

    class Meta:
        model = Customer
        fields = [
            'customer_type',
            'full_name',
            'company_name',
            'email',
            'alternate_email',
            'address',
            'city',
            'area',
            'preferred_contact_method',
            'needs_feedback',
            'notes',
        ]
        widgets = {
            'customer_type': forms.Select(attrs={'class': 'eo-form-control'}),
            'full_name': forms.TextInput(attrs={
                'class': 'eo-form-control',
                'placeholder': 'e.g. Mariama Jallow',
                'autocomplete': 'off',
            }),
            'company_name': forms.TextInput(attrs={
                'class': 'eo-form-control',
                'placeholder': 'Optional — only for business contacts',
                'autocomplete': 'off',
            }),
            'email': forms.EmailInput(attrs={
                'class': 'eo-form-control',
                'placeholder': '[email protected]',
            }),
            'alternate_email': forms.EmailInput(attrs={
                'class': 'eo-form-control',
                'placeholder': 'Optional secondary email',
            }),
            'address': forms.Textarea(attrs={
                'class': 'eo-form-control',
                'rows': 2,
                'placeholder': 'Street / building / PO box',
            }),
            'city': forms.TextInput(attrs={
                'class': 'eo-form-control',
                'placeholder': 'e.g. Banjul',
            }),
            'area': forms.TextInput(attrs={
                'class': 'eo-form-control',
                'placeholder': 'e.g. Bakau',
            }),
            'preferred_contact_method': forms.Select(attrs={'class': 'eo-form-control'}),
            'notes': forms.Textarea(attrs={
                'class': 'eo-form-control',
                'rows': 3,
                'placeholder': 'Anything else worth remembering — preferences, '
                               'history, special handling…',
            }),
        }

    def clean(self):
        cleaned = super().clean()
        ctype = cleaned.get('customer_type')
        full_name = (cleaned.get('full_name') or '').strip()
        company_name = (cleaned.get('company_name') or '').strip()

        # For businesses we want at LEAST a company name. Full name then
        # represents the contact person at that company.
        if ctype == 'business' and not company_name:
            self.add_error(
                'company_name',
                'Company name is required for business contacts.',
            )
        if not full_name:
            self.add_error(
                'full_name',
                'A contact name is required (the person we’ll speak to).',
            )
        return cleaned


class CustomerPhoneForm(forms.ModelForm):
    """One row in the inline phones formset."""
    class Meta:
        model = CustomerPhone
        fields = ['phone_type', 'phone_number', 'is_primary', 'is_active']
        widgets = {
            'phone_type': forms.Select(attrs={'class': 'eo-form-control eo-form-control-sm'}),
            'phone_number': forms.TextInput(attrs={
                'class': 'eo-form-control eo-form-control-sm',
                'placeholder': '+220 XXX XXXX',
                'autocomplete': 'off',
            }),
        }


# Inline formset: 1 phone shown by default, up to 5 may be added per submit.
CustomerPhoneFormSet = inlineformset_factory(
    Customer,
    CustomerPhone,
    form=CustomerPhoneForm,
    fields=['phone_type', 'phone_number', 'is_primary', 'is_active'],
    extra=1,
    max_num=5,
    can_delete=True,
)
