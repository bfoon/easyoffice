"""apps/orders/forms.py — minimal forms used by the internal agent UI."""
from decimal import Decimal
from django import forms

from .models import SalesOrder, SalesOrderItem


class OrderHeaderForm(forms.ModelForm):
    """Top-of-page form for creating/editing an order's header info."""
    class Meta:
        model  = SalesOrder
        fields = [
            'customer',
            'contact_name', 'contact_phone', 'contact_email',
            'delivery_address',
            'notes',
            'currency', 'tax_rate', 'discount_amount',
        ]
        widgets = {
            'delivery_address': forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea'}),
            'notes':            forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea'}),
        }


class OrderItemForm(forms.ModelForm):
    class Meta:
        model  = SalesOrderItem
        fields = ['description', 'quantity', 'unit_price']
        widgets = {
            'description': forms.TextInput(attrs={'class': 'eo-input'}),
            'quantity':    forms.NumberInput(attrs={'class': 'eo-input', 'step': '0.01', 'min': '0'}),
            'unit_price':  forms.NumberInput(attrs={'class': 'eo-input', 'step': '0.01', 'min': '0'}),
        }


class CancelOrderForm(forms.Form):
    reason = forms.CharField(
        max_length=300, required=False,
        widget=forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea',
                                      'placeholder': 'Optional cancellation reason…'}),
    )


class AttachCustomerForm(forms.Form):
    """Used when an order arrived without a customer — agent picks one."""
    customer = forms.ModelChoiceField(queryset=None, required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Lazy import — avoids circular at app loading
        from apps.customer_service.models import Customer
        self.fields['customer'].queryset = Customer.objects.all().order_by('full_name')
