"""apps/inventory/forms.py — minimal forms for the internal UI."""
from decimal import Decimal
from django import forms

from .models import (
    Asset, AssetMaintenance, Category, Location, Product, StockRequest,
    StockTake, Supplier,
)


# ── Catalog ────────────────────────────────────────────────────────────────

class ProductForm(forms.ModelForm):
    class Meta:
        model  = Product
        fields = [
            'sku', 'barcode', 'name', 'description', 'kind', 'category',
            'image', 'unit_label', 'cost_price', 'sell_price', 'currency',
            'is_sellable', 'is_purchasable', 'is_for_internal',
            'track_serial', 'track_batch',
            'reorder_point', 'reorder_qty', 'preferred_supplier',
        ]
        widgets = {
            'description':  forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea'}),
            'sku':          forms.TextInput(attrs={'class': 'eo-input'}),
            'barcode':      forms.TextInput(attrs={'class': 'eo-input'}),
            'name':         forms.TextInput(attrs={'class': 'eo-input'}),
            'unit_label':   forms.TextInput(attrs={'class': 'eo-input'}),
            'cost_price':   forms.NumberInput(attrs={'class': 'eo-input', 'step': '0.01'}),
            'sell_price':   forms.NumberInput(attrs={'class': 'eo-input', 'step': '0.01'}),
            'currency':     forms.TextInput(attrs={'class': 'eo-input'}),
            'reorder_point': forms.NumberInput(attrs={'class': 'eo-input', 'step': '0.01'}),
            'reorder_qty':   forms.NumberInput(attrs={'class': 'eo-input', 'step': '0.01'}),
        }


class LocationForm(forms.ModelForm):
    class Meta:
        model  = Location
        fields = ['code', 'name', 'kind', 'address', 'is_sellable', 'is_active', 'department', 'notes']
        widgets = {
            'address': forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea'}),
            'notes':   forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea'}),
        }


class CategoryForm(forms.ModelForm):
    class Meta:
        model  = Category
        fields = ['name', 'parent', 'icon', 'is_active']


class SupplierForm(forms.ModelForm):
    class Meta:
        model  = Supplier
        fields = ['name', 'contact_name', 'phone', 'email', 'address', 'tax_id', 'notes', 'is_active']
        widgets = {
            'address': forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea'}),
            'notes':   forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea'}),
        }


# ── Stock movements ────────────────────────────────────────────────────────

class ReceiveStockForm(forms.Form):
    """Bring stock in from a supplier."""
    product     = forms.ModelChoiceField(queryset=Product.objects.filter(is_active=True))
    location    = forms.ModelChoiceField(queryset=Location.objects.filter(is_active=True))
    quantity    = forms.DecimalField(min_value=Decimal('0.01'), max_digits=14, decimal_places=2)
    cost_price  = forms.DecimalField(min_value=Decimal('0'), required=False, max_digits=12, decimal_places=2)
    supplier    = forms.ModelChoiceField(queryset=Supplier.objects.filter(is_active=True), required=False)
    batch_no    = forms.CharField(max_length=50, required=False)
    expiry_date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
    notes       = forms.CharField(required=False, max_length=300, widget=forms.Textarea(attrs={'rows': 2}))


class IssueStockForm(forms.Form):
    """Generic outbound — used for internal use, scrap, ad-hoc sales."""
    KIND_CHOICES = [
        ('internal_use', 'Internal Use'),
        ('sale',         'Sold (cash)'),
        ('return_out',   'Return to Supplier'),
        ('scrap',        'Scrap / Damaged'),
        ('adjust_out',   'Stock Adjustment (Remove)'),
    ]
    product  = forms.ModelChoiceField(queryset=Product.objects.filter(is_active=True))
    location = forms.ModelChoiceField(queryset=Location.objects.filter(is_active=True))
    quantity = forms.DecimalField(min_value=Decimal('0.01'), max_digits=14, decimal_places=2)
    kind     = forms.ChoiceField(choices=KIND_CHOICES, initial='internal_use')
    notes    = forms.CharField(required=False, max_length=300, widget=forms.Textarea(attrs={'rows': 2}))


class TransferStockForm(forms.Form):
    product       = forms.ModelChoiceField(queryset=Product.objects.filter(is_active=True))
    from_location = forms.ModelChoiceField(queryset=Location.objects.filter(is_active=True))
    to_location   = forms.ModelChoiceField(queryset=Location.objects.filter(is_active=True))
    quantity      = forms.DecimalField(min_value=Decimal('0.01'), max_digits=14, decimal_places=2)
    notes         = forms.CharField(required=False, max_length=300, widget=forms.Textarea(attrs={'rows': 2}))

    def clean(self):
        cleaned = super().clean()
        if (cleaned.get('from_location') and cleaned.get('to_location')
                and cleaned['from_location'].pk == cleaned['to_location'].pk):
            raise forms.ValidationError('Source and destination must be different.')
        return cleaned


# ── Assets ─────────────────────────────────────────────────────────────────

class AssetForm(forms.ModelForm):
    class Meta:
        model  = Asset
        fields = [
            'tag', 'name', 'description', 'category', 'serial_number',
            'model_number', 'manufacturer',
            'location', 'department',
            'condition',
            'purchase_date', 'purchase_cost', 'currency', 'supplier',
            'warranty_until', 'useful_life_months', 'salvage_value',
            'image', 'notes',
        ]
        widgets = {
            'description':    forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea'}),
            'notes':          forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea'}),
            'purchase_date':  forms.DateInput(attrs={'type': 'date'}),
            'warranty_until': forms.DateInput(attrs={'type': 'date'}),
        }


class AssetAssignForm(forms.Form):
    assignee = forms.ModelChoiceField(queryset=None, required=True)
    purpose  = forms.CharField(max_length=300, required=False,
                               widget=forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.fields['assignee'].queryset = User.objects.filter(is_active=True).order_by(
            'first_name', 'last_name', 'username',
        )


class AssetReturnForm(forms.Form):
    condition = forms.ChoiceField(choices=Asset.Condition.choices, required=False)
    note      = forms.CharField(required=False, max_length=400,
                                widget=forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea'}))


class AssetMaintenanceForm(forms.ModelForm):
    class Meta:
        model  = AssetMaintenance
        fields = ['kind', 'description', 'cost', 'performed_by', 'performed_on', 'next_due', 'attachment']
        widgets = {
            'description':  forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea'}),
            'performed_on': forms.DateInput(attrs={'type': 'date'}),
            'next_due':     forms.DateInput(attrs={'type': 'date'}),
        }


# ── Stock-take & requests ──────────────────────────────────────────────────

class StockTakeForm(forms.ModelForm):
    class Meta:
        model  = StockTake
        fields = ['location', 'notes']
        widgets = {'notes': forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea'})}


class StockRequestForm(forms.ModelForm):
    class Meta:
        model  = StockRequest
        fields = ['department', 'location', 'purpose']
        widgets = {'purpose': forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea'})}
