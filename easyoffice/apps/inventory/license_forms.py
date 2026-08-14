"""
apps/inventory/license_forms.py
───────────────────────────────
Forms for the licence screens.

Two things worth knowing:

  • `LicenseForm` takes a `user` kwarg. Commercial fields (cost, selling
    price) and the licence key are removed from the form for anyone who
    doesn't hold the matching access level, so the permission rules can't
    be bypassed by POSTing a hidden field.

  • Picking a licence type pre-fills term, cost and price when those are
    left at zero — the storekeeper types the seat count and nothing else.
"""
from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone

from .license_models import BillingModel, License, LicenseType
from .models import Asset, Category, Product, Supplier
from .permissions import can_see_license_key, can_see_license_money

User = get_user_model()

INPUT = {'class': 'inv-input'}
SELECT = {'class': 'inv-select'}
AREA = {'class': 'inv-textarea', 'rows': 2}
DATE = {'class': 'inv-input', 'type': 'date'}
MONEY = {'class': 'inv-input', 'step': '0.01', 'min': '0'}


# ════════════════════════════════════════════════════════════════════════════
# Catalogue
# ════════════════════════════════════════════════════════════════════════════

class LicenseTypeForm(forms.ModelForm):
    class Meta:
        model = LicenseType
        fields = [
            'code', 'name', 'description', 'vendor', 'category', 'product',
            'billing_model', 'billing_cycle', 'default_term_months',
            'default_unit_cost', 'default_unit_price', 'currency',
            'support_url', 'notes', 'is_active',
        ]
        widgets = {
            'code':               forms.TextInput(attrs=INPUT),
            'name':               forms.TextInput(attrs=INPUT),
            'description':        forms.Textarea(attrs=AREA),
            'vendor':             forms.Select(attrs=SELECT),
            'category':           forms.Select(attrs=SELECT),
            'product':            forms.Select(attrs=SELECT),
            'billing_model':      forms.Select(attrs=SELECT),
            'billing_cycle':      forms.Select(attrs=SELECT),
            'default_term_months': forms.NumberInput(attrs={'class': 'inv-input', 'min': '0'}),
            'default_unit_cost':  forms.NumberInput(attrs=MONEY),
            'default_unit_price': forms.NumberInput(attrs=MONEY),
            'currency':           forms.TextInput(attrs=INPUT),
            'support_url':        forms.URLInput(attrs=INPUT),
            'notes':              forms.Textarea(attrs=AREA),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['vendor'].queryset = Supplier.objects.filter(is_active=True).order_by('name')
        self.fields['category'].queryset = Category.objects.filter(is_active=True)
        self.fields['product'].queryset = Product.objects.filter(is_active=True).order_by('name')
        self.fields['code'].help_text = 'Short, stable, and unique — you will search on it.'

    def clean_code(self):
        return (self.cleaned_data.get('code') or '').strip().upper()


# ════════════════════════════════════════════════════════════════════════════
# The licence
# ════════════════════════════════════════════════════════════════════════════

class LicenseForm(forms.ModelForm):
    class Meta:
        model = License
        fields = [
            # what
            'name', 'license_type', 'vendor',
            # who for
            'holder_kind', 'holder_user', 'customer_name', 'customer_email',
            'customer_ref', 'department', 'owner',
            # credentials
            'license_key', 'account_email', 'portal_url', 'attachment',
            # money
            'billing_model', 'billing_cycle', 'seats',
            'unit_cost', 'unit_price', 'setup_cost', 'setup_price',
            'currency', 'purchase_ref',
            # term
            'start_date', 'end_date', 'is_perpetual', 'auto_renew',
            'renewal_term_months', 'grace_days',
            # alerts
            'alerts_enabled', 'reminder_days', 'notify_customer',
            # meta
            'status', 'notes',
        ]
        widgets = {
            'name':           forms.TextInput(attrs={**INPUT, 'placeholder': 'e.g. Microsoft 365 Business Standard — head office'}),
            'license_type':   forms.Select(attrs=SELECT),
            'vendor':         forms.Select(attrs=SELECT),
            'holder_kind':    forms.Select(attrs={**SELECT, 'id': 'id_holder_kind'}),
            'holder_user':    forms.Select(attrs=SELECT),
            'customer_name':  forms.TextInput(attrs=INPUT),
            'customer_email': forms.EmailInput(attrs=INPUT),
            'customer_ref':   forms.TextInput(attrs=INPUT),
            'department':     forms.Select(attrs=SELECT),
            'owner':          forms.Select(attrs=SELECT),
            'license_key':    forms.TextInput(attrs={**INPUT, 'autocomplete': 'off'}),
            'account_email':  forms.EmailInput(attrs=INPUT),
            'portal_url':     forms.URLInput(attrs=INPUT),
            'billing_model':  forms.Select(attrs=SELECT),
            'billing_cycle':  forms.Select(attrs=SELECT),
            'seats':          forms.NumberInput(attrs={'class': 'inv-input', 'min': '1'}),
            'unit_cost':      forms.NumberInput(attrs=MONEY),
            'unit_price':     forms.NumberInput(attrs=MONEY),
            'setup_cost':     forms.NumberInput(attrs=MONEY),
            'setup_price':    forms.NumberInput(attrs=MONEY),
            'currency':       forms.TextInput(attrs={**INPUT, 'maxlength': '3'}),
            'purchase_ref':   forms.TextInput(attrs=INPUT),
            'start_date':     forms.DateInput(attrs=DATE, format='%Y-%m-%d'),
            'end_date':       forms.DateInput(attrs=DATE, format='%Y-%m-%d'),
            'renewal_term_months': forms.NumberInput(attrs={'class': 'inv-input', 'min': '0'}),
            'grace_days':     forms.NumberInput(attrs={'class': 'inv-input', 'min': '0'}),
            'reminder_days':  forms.TextInput(attrs={**INPUT, 'placeholder': '90,60,30,14,7,1'}),
            'status':         forms.Select(attrs=SELECT),
            'notes':          forms.Textarea(attrs={**AREA, 'rows': 3}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

        staff = User.objects.filter(is_active=True).order_by(
            'first_name', 'last_name', 'username')
        self.fields['holder_user'].queryset = staff
        self.fields['owner'].queryset = staff
        self.fields['owner'].help_text = 'Who gets the renewal reminders first.'
        self.fields['vendor'].queryset = Supplier.objects.filter(is_active=True).order_by('name')
        self.fields['license_type'].queryset = LicenseType.objects.filter(
            is_active=True).order_by('name')
        self.fields['end_date'].input_formats = ['%Y-%m-%d']
        self.fields['start_date'].input_formats = ['%Y-%m-%d']

        # Field-level access: strip what this person may not set.
        if user is not None and not can_see_license_money(user):
            for f in ('unit_cost', 'unit_price', 'setup_cost', 'setup_price',
                      'purchase_ref'):
                self.fields.pop(f, None)
        if user is not None and not can_see_license_key(user):
            self.fields.pop('license_key', None)

    # ── Field groups for the template ───────────────────────────────────────
    # Exposed as properties (templates can't call methods with arguments) and
    # they skip anything the permission check removed above, so a viewer's
    # form has no empty labels hanging around.

    GROUPS = {
        'what':         ['name', 'license_type', 'vendor'],
        'holder':       ['holder_kind', 'holder_user', 'customer_name',
                         'customer_email', 'customer_ref', 'department', 'owner'],
        'money':        ['billing_model', 'billing_cycle', 'seats', 'currency',
                         'unit_cost', 'unit_price', 'setup_cost', 'setup_price',
                         'purchase_ref'],
        'term':         ['start_date', 'end_date', 'renewal_term_months', 'grace_days'],
        'term_flags':   ['is_perpetual', 'auto_renew'],
        'alert_flags':  ['alerts_enabled', 'notify_customer'],
        'alerts':       ['reminder_days'],
        'credentials':  ['license_key', 'account_email', 'portal_url', 'attachment'],
        'meta':         ['status', 'notes'],
    }

    def _group(self, key):
        return [self[n] for n in self.GROUPS[key] if n in self.fields]

    @property
    def fields_what(self):        return self._group('what')

    @property
    def fields_holder(self):      return self._group('holder')

    @property
    def fields_money(self):       return self._group('money')

    @property
    def fields_term(self):        return self._group('term')

    @property
    def fields_term_flags(self):  return self._group('term_flags')

    @property
    def fields_alert_flags(self): return self._group('alert_flags')

    @property
    def fields_alerts(self):      return self._group('alerts')

    @property
    def fields_credentials(self): return self._group('credentials')

    @property
    def fields_meta(self):        return self._group('meta')

    def clean(self):
        cleaned = super().clean()
        lt = cleaned.get('license_type')

        # Pre-fill from the catalogue when the commercials were left blank.
        if lt:
            if not cleaned.get('vendor') and lt.vendor_id:
                cleaned['vendor'] = lt.vendor
            if 'unit_cost' in self.fields and not cleaned.get('unit_cost'):
                cleaned['unit_cost'] = lt.default_unit_cost
            if 'unit_price' in self.fields and not cleaned.get('unit_price'):
                cleaned['unit_price'] = lt.default_unit_price
            if not cleaned.get('billing_model'):
                cleaned['billing_model'] = lt.billing_model
            if not cleaned.get('currency'):
                cleaned['currency'] = lt.currency
            if not cleaned.get('end_date') and not cleaned.get('is_perpetual'):
                from .license_services import add_months
                start = cleaned.get('start_date') or timezone.localdate()
                if lt.default_term_months:
                    cleaned['end_date'] = add_months(start, lt.default_term_months)

        # Same rules the model enforces, surfaced next to the right field.
        perpetual = cleaned.get('is_perpetual')
        end = cleaned.get('end_date')
        start = cleaned.get('start_date')
        if not perpetual and not end:
            self.add_error('end_date', 'Set an end date, or tick "never expires".')
        if end and start and end < start:
            self.add_error('end_date', 'The end date is before the start date.')

        kind = cleaned.get('holder_kind')
        if kind == License.Holder.USER and not cleaned.get('holder_user'):
            self.add_error('holder_user', 'Pick the staff member this licence is for.')
        if kind == License.Holder.CUSTOMER and not (cleaned.get('customer_name') or '').strip():
            self.add_error('customer_name', 'Name the customer this licence is for.')
        if cleaned.get('notify_customer') and not cleaned.get('customer_email'):
            self.add_error('customer_email',
                           'Add a contact email, or turn off the customer copy.')

        seats = cleaned.get('seats') or 0
        if self.instance.pk and seats:
            in_use = self.instance.seats_assigned
            if seats < in_use:
                self.add_error(
                    'seats',
                    f'{in_use} seat(s) are assigned — release some before '
                    f'reducing the count to {seats}.')

        raw_days = (cleaned.get('reminder_days') or '').strip()
        if raw_days:
            bad = [c.strip() for c in raw_days.replace(';', ',').split(',')
                   if c.strip() and not c.strip().isdigit()]
            if bad:
                self.add_error('reminder_days',
                               f'Whole numbers of days only — check: {", ".join(bad)}')
        return cleaned


# ════════════════════════════════════════════════════════════════════════════
# Seats
# ════════════════════════════════════════════════════════════════════════════

class LicenseSeatForm(forms.Form):
    """Assign one seat, to a staff account or to a named outsider."""

    MODE_CHOICES = [
        ('user',     'A staff member'),
        ('external', 'Someone outside EasyOffice'),
        ('device',   'A device'),
    ]

    mode         = forms.ChoiceField(
        choices=MODE_CHOICES, initial='user', widget=forms.Select(attrs=SELECT),
        label='Assign to',
    )
    user         = forms.ModelChoiceField(
        queryset=User.objects.none(), required=False,
        widget=forms.Select(attrs=SELECT), label='Staff member',
    )
    person_name  = forms.CharField(
        max_length=160, required=False, widget=forms.TextInput(attrs=INPUT),
        label='Name',
    )
    person_email = forms.EmailField(
        required=False, widget=forms.EmailInput(attrs=INPUT), label='Email',
        help_text='They get an email confirming the seat.',
    )
    device_label = forms.CharField(
        max_length=120, required=False, widget=forms.TextInput(attrs=INPUT),
        label='Device', help_text='Machine name, or the asset tag.',
    )
    asset        = forms.ModelChoiceField(
        queryset=Asset.objects.none(), required=False,
        widget=forms.Select(attrs=SELECT), label='Linked asset',
    )
    cost_override = forms.DecimalField(
        required=False, max_digits=14, decimal_places=2,
        widget=forms.NumberInput(attrs=MONEY), label='Cost for this seat',
        help_text='Only if this seat is priced differently from the rest.',
    )
    note         = forms.CharField(
        max_length=200, required=False, widget=forms.TextInput(attrs=INPUT),
    )

    def __init__(self, *args, license_obj=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.license_obj = license_obj
        assigned = []
        if license_obj is not None:
            assigned = list(
                license_obj.assignments
                .filter(released_at__isnull=True, user__isnull=False)
                .values_list('user_id', flat=True)
            )
            if license_obj.billing_model == BillingModel.PER_DEVICE:
                self.fields['mode'].initial = 'device'
        self.fields['user'].queryset = (
            User.objects.filter(is_active=True)
            .exclude(id__in=assigned)
            .order_by('first_name', 'last_name', 'username')
        )
        self.fields['asset'].queryset = Asset.objects.filter(
            is_active=True).order_by('tag')

    def clean(self):
        cleaned = super().clean()
        mode = cleaned.get('mode')
        if mode == 'user' and not cleaned.get('user'):
            self.add_error('user', 'Pick the person taking the seat.')
        if mode == 'external' and not (cleaned.get('person_name') or '').strip():
            self.add_error('person_name', 'Give a name for this seat.')
        if mode == 'device' and not ((cleaned.get('device_label') or '').strip()
                                     or cleaned.get('asset')):
            self.add_error('device_label', 'Name the device, or link an asset.')
        return cleaned


class LicenseSeatReleaseForm(forms.Form):
    reason = forms.CharField(
        max_length=200, required=False, widget=forms.TextInput(attrs=INPUT),
        label='Why is the seat coming back?',
    )


# ════════════════════════════════════════════════════════════════════════════
# Renewal & status
# ════════════════════════════════════════════════════════════════════════════

class LicenseRenewForm(forms.Form):
    term_months = forms.IntegerField(
        min_value=1, max_value=120, required=False,
        widget=forms.NumberInput(attrs={'class': 'inv-input', 'min': '1'}),
        label='Extend by (months)',
        help_text='Leave blank to use the licence default.',
    )
    new_end     = forms.DateField(
        required=False, widget=forms.DateInput(attrs=DATE, format='%Y-%m-%d'),
        input_formats=['%Y-%m-%d'], label='Or set the exact end date',
    )
    seats       = forms.IntegerField(
        min_value=1, required=False,
        widget=forms.NumberInput(attrs={'class': 'inv-input', 'min': '1'}),
        label='Seats for the new term',
    )
    unit_cost   = forms.DecimalField(
        required=False, max_digits=14, decimal_places=2,
        widget=forms.NumberInput(attrs=MONEY), label='New cost per seat',
        help_text='Leave blank to keep the current rate.',
    )
    unit_price  = forms.DecimalField(
        required=False, max_digits=14, decimal_places=2,
        widget=forms.NumberInput(attrs=MONEY), label='New selling price per seat',
    )
    invoice_ref = forms.CharField(
        max_length=100, required=False, widget=forms.TextInput(attrs=INPUT),
        label='Invoice / PO reference',
    )
    notes       = forms.CharField(
        max_length=300, required=False, widget=forms.Textarea(attrs=AREA),
    )

    def __init__(self, *args, license_obj=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.license_obj = license_obj
        if license_obj is not None:
            self.fields['term_months'].initial = license_obj.renewal_term_months
            self.fields['seats'].initial = license_obj.seats
            self.fields['unit_cost'].initial = license_obj.unit_cost
            self.fields['unit_price'].initial = license_obj.unit_price

    def clean(self):
        cleaned = super().clean()
        lic = self.license_obj
        new_end = cleaned.get('new_end')
        if new_end and lic and lic.end_date and new_end <= lic.end_date:
            self.add_error('new_end',
                           f'Pick a date after the current end ({lic.end_date}).')
        if not cleaned.get('term_months') and not new_end:
            if not (lic and lic.renewal_term_months):
                self.add_error('term_months', 'Set a term, or an exact end date.')
        return cleaned


class LicenseStatusForm(forms.Form):
    ACTIONS = [
        ('suspend',    'Suspend'),
        ('reactivate', 'Reactivate'),
        ('cancel',     'Cancel'),
    ]
    action = forms.ChoiceField(choices=ACTIONS, widget=forms.Select(attrs=SELECT))
    reason = forms.CharField(
        max_length=300, required=False, widget=forms.Textarea(attrs=AREA),
    )
    release_seats = forms.BooleanField(
        required=False, initial=True,
        label='Release every seat (cancel only)',
    )
