from django import forms
from .models import InvoiceDocument, DocType, InvoiceTemplate


class InvoiceMetadataForm(forms.ModelForm):
    """Everything except line items and layout."""
    class Meta:
        model = InvoiceDocument
        fields = [
            'doc_type',
            'client_name', 'client_address', 'client_email', 'ship_to_address',
            'po_reference',
            'invoice_date', 'due_date', 'payment_terms',
            'currency', 'tax_rate', 'discount_amount',
            'bank_details', 'notes',
        ]
        widgets = {
            'client_address':  forms.Textarea(attrs={'rows': 3, 'class': 'eo-textarea'}),
            'ship_to_address': forms.Textarea(attrs={'rows': 3, 'class': 'eo-textarea'}),
            'bank_details':    forms.Textarea(attrs={'rows': 3, 'class': 'eo-textarea'}),
            'notes':           forms.Textarea(attrs={'rows': 2, 'class': 'eo-textarea'}),
            'invoice_date':    forms.DateInput(attrs={'type': 'date', 'class': 'eo-input'}),
            'due_date':        forms.DateInput(attrs={'type': 'date', 'class': 'eo-input'}),
        }


class InvoiceInitForm(forms.Form):
    """Used on 'New Invoice' landing to pick letterhead + doc type."""
    doc_type = forms.ChoiceField(choices=DocType.choices, initial=DocType.INVOICE)
    letterhead_id = forms.UUIDField()


class TemplateForm(forms.ModelForm):
    """
    Create or edit a template. Used both from the builder's "Save as template"
    modal and the standalone template edit page.
    """
    # Optional — leave blank to allow the user to switch doc type when using
    LOCK_DOC_TYPE_CHOICES = [('', "Don't lock doc type (user can switch)")] + list(DocType.choices)
    doc_type = forms.ChoiceField(
        choices=LOCK_DOC_TYPE_CHOICES, required=False,
        label='Lock document type?',
    )

    class Meta:
        model = InvoiceTemplate
        fields = [
            'name', 'description', 'visibility',
            'locked_layout', 'doc_type',
            'default_currency', 'default_tax_rate',
            'default_payment_terms', 'default_bank_details', 'default_notes',
        ]
        widgets = {
            'description':          forms.TextInput(attrs={'class': 'eo-input'}),
            'default_bank_details': forms.Textarea(attrs={'rows': 3}),
            'default_notes':        forms.Textarea(attrs={'rows': 2}),
        }