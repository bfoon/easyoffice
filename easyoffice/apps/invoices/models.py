"""
Invoice generator models.

Three models:
    - InvoiceCounter   : per (doc_type, year) sequence, thread-safe via select_for_update
    - InvoiceDocument  : one row per invoice / proforma / delivery note
    - InvoiceLineItem  : line items for an invoice
    - InvoiceTemplate  : reusable invoice blueprint
"""
import uuid
from decimal import Decimal
from django.db import models, transaction
from django.utils import timezone
from apps.core.models import User
from apps.files.models import SharedFile


# ── Doc-type helpers ─────────────────────────────────────────────────────────

class DocType(models.TextChoices):
    INVOICE       = 'invoice',       'Invoice'
    PROFORMA      = 'proforma',      'Proforma Invoice'
    DELIVERY_NOTE = 'delivery_note', 'Delivery Note'


DOC_TYPE_PREFIX = {
    DocType.INVOICE:       'INV',
    DocType.PROFORMA:      'PRO',
    DocType.DELIVERY_NOTE: 'DN',
}

DOC_TYPE_TITLE = {
    DocType.INVOICE:       'INVOICE',
    DocType.PROFORMA:      'PROFORMA INVOICE',
    DocType.DELIVERY_NOTE: 'DELIVERY NOTE',
}


# ── Counter ──────────────────────────────────────────────────────────────────

class InvoiceCounter(models.Model):
    """
    Per (doc_type, year) sequence generator.
    Resets automatically every January — a new row is created on first use each year.
    """
    doc_type      = models.CharField(max_length=20, choices=DocType.choices)
    year          = models.PositiveIntegerField()
    last_sequence = models.PositiveIntegerField(default=0)
    updated_at    = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('doc_type', 'year')
        ordering = ['-year', 'doc_type']

    def __str__(self):
        return f'{self.get_doc_type_display()} {self.year} — last #{self.last_sequence}'

    @classmethod
    def allocate_next(cls, doc_type, year=None):
        """
        Thread-safely allocate the next sequence for (doc_type, year).
        Returns an int (the new last_sequence) — caller formats the full number.
        """
        year = year or timezone.now().year
        with transaction.atomic():
            counter, _ = cls.objects.select_for_update().get_or_create(
                doc_type=doc_type, year=year,
                defaults={'last_sequence': 0},
            )
            counter.last_sequence += 1
            counter.save(update_fields=['last_sequence', 'updated_at'])
            return counter.last_sequence

    @classmethod
    def peek_next(cls, doc_type, year=None):
        """Returns what the next sequence would be, WITHOUT incrementing."""
        year = year or timezone.now().year
        counter = cls.objects.filter(doc_type=doc_type, year=year).first()
        return (counter.last_sequence if counter else 0) + 1


# ── Invoice ──────────────────────────────────────────────────────────────────

class InvoiceDocument(models.Model):
    class Status(models.TextChoices):
        DRAFT     = 'draft',     'Draft'
        FINALIZED = 'finalized', 'Finalized'
        VOIDED    = 'voided',    'Voided'

    # ── Identification ────────────────────────────────────────────────────────
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    doc_type    = models.CharField(max_length=20, choices=DocType.choices, default=DocType.INVOICE)
    number      = models.CharField(max_length=40, blank=True, db_index=True,
                                   help_text='Auto-generated on finalize, e.g. INV-2026-0001')
    sequence    = models.PositiveIntegerField(null=True, blank=True,
                                              help_text='Sequence portion of number')
    year        = models.PositiveIntegerField(null=True, blank=True)

    # ── Letterhead / template ────────────────────────────────────────────────
    letterhead  = models.ForeignKey(
        SharedFile, on_delete=models.PROTECT,
        related_name='invoice_letterheads',
        help_text='PDF from files app used as background/letterhead',
    )
    layout_json = models.JSONField(
        default=dict, blank=True,
        help_text='Custom positions for invoice blocks (empty = use defaults)',
    )

    # ── Status ────────────────────────────────────────────────────────────────
    status      = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    # ── Client / recipient ────────────────────────────────────────────────────
    client_name     = models.CharField(max_length=200, blank=True)
    client_address  = models.TextField(blank=True)
    client_email    = models.EmailField(blank=True)
    ship_to_address = models.TextField(blank=True, help_text='Leave blank to use client address')
    po_reference    = models.CharField(max_length=100, blank=True)

    # ── Dates & terms ─────────────────────────────────────────────────────────
    invoice_date    = models.DateField(default=timezone.localdate)
    due_date        = models.DateField(null=True, blank=True)
    payment_terms   = models.CharField(max_length=200, blank=True, default='Net 30')

    # ── Money ─────────────────────────────────────────────────────────────────
    currency        = models.CharField(max_length=3, default='GMD')
    tax_rate        = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'),
                                           help_text='VAT/tax rate as a percentage')
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    subtotal        = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    tax_amount      = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total           = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))

    # ── Footer data ───────────────────────────────────────────────────────────
    bank_details    = models.TextField(blank=True, help_text='Bank name, account number, SWIFT, etc.')
    notes           = models.TextField(blank=True)

    # ── Output / audit ────────────────────────────────────────────────────────
    generated_pdf   = models.ForeignKey(
        SharedFile, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='invoice_generated_from',
        help_text='The SharedFile created when the invoice was finalized',
    )
    created_by      = models.ForeignKey(User, on_delete=models.PROTECT,
                                         related_name='created_invoice_docs')
    finalized_at    = models.DateTimeField(null=True, blank=True)
    voided_at       = models.DateTimeField(null=True, blank=True)
    voided_by       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name='voided_invoice_docs')
    void_reason     = models.CharField(max_length=300, blank=True)

    # ── Payment / receivable bridge ──────────────────────────────────────────
    # When a billable invoice (doc_type=INVOICE) is finalized we auto-create a
    # matching IncomingPaymentRequest in the finance app so it appears in the
    # AR ageing, revenue figures, cash-flow statement, etc. Proformas and
    # Delivery Notes are NOT billable and never get a receivable.
    linked_receivable = models.OneToOneField(
        'finance.IncomingPaymentRequest',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='source_invoice_document',
        help_text='The IncomingPaymentRequest auto-created when this invoice was finalized.',
    )
    # Denormalized "is paid" markers — kept on the invoice itself so the
    # invoice list/detail can show paid status without a join, and so we
    # have a record even if the receivable row gets deleted later.
    paid_at         = models.DateTimeField(null=True, blank=True, db_index=True)
    paid_by         = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='invoices_marked_paid',
        help_text='User who marked this invoice as paid.',
    )
    paid_amount     = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True,
        help_text='Actual amount received (may differ from total on partial/overpayment).',
    )
    payment_method  = models.CharField(
        max_length=30, blank=True, default='',
        help_text='Bank transfer, cash, cheque, mobile money, etc.',
    )
    payment_reference = models.CharField(
        max_length=100, blank=True, default='',
        help_text='Bank reference / cheque number / mobile money txn id.',
    )
    # Link to the Payment ledger entry generated when marked paid.
    linked_payment  = models.ForeignKey(
        'finance.Payment',
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='source_invoice_documents',
        help_text='The Payment ledger entry recorded when this invoice was paid.',
    )

    # Template this invoice was created from (if any). SET_NULL on template
    # delete — so the invoice keeps its data and just loses the template link.
    template        = models.ForeignKey(
        'InvoiceTemplate', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='invoices_using',
    )
    # Conversion chain: Proforma → Invoice → Delivery Note (one-to-one).
    # OneToOneField at the DB level prevents two docs from pointing at the
    # same source. SET_NULL on source delete — the child keeps its data.
    converted_from  = models.OneToOneField(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='converted_to',
        help_text='The document that was converted to create this one (e.g. Proforma → Invoice).',
    )
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['doc_type', 'year', 'sequence']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return self.number or f'Draft {self.get_doc_type_display()} ({self.id})'

    # ── Helpers ───────────────────────────────────────────────────────────────
    @property
    def is_draft(self):
        return self.status == self.Status.DRAFT

    @property
    def is_finalized(self):
        return self.status == self.Status.FINALIZED

    @property
    def is_voided(self):
        return self.status == self.Status.VOIDED

    @property
    def is_layout_locked(self):
        """True if this invoice was made from a template that locks layout."""
        return bool(self.template and self.template.locked_layout)

    @property
    def is_doc_type_locked(self):
        """True if this invoice was made from a template that locks doc type."""
        return bool(self.template and self.template.doc_type)

    # ── Conversion chain helpers ────────────────────────────────────────────
    @property
    def is_converted_source(self):
        """True if this doc has been converted into another (child exists)."""
        # `converted_to` is the reverse accessor of the OneToOneField. When no
        # child exists, accessing it raises DoesNotExist.
        try:
            return self.converted_to is not None
        except InvoiceDocument.DoesNotExist:
            return False

    @property
    def converted_child(self):
        """Return the child doc (if any), or None without raising."""
        try:
            return self.converted_to
        except InvoiceDocument.DoesNotExist:
            return None

    @property
    def next_doc_type(self):
        """The doc type this one can be converted to, or None if terminal."""
        return {
            DocType.PROFORMA:      DocType.INVOICE,
            DocType.INVOICE:       DocType.DELIVERY_NOTE,
            DocType.DELIVERY_NOTE: None,
        }.get(self.doc_type)

    @property
    def can_convert(self):
        """True if this doc can be converted to a next-stage doc right now."""
        return (
            self.is_finalized
            and not self.is_converted_source
            and self.next_doc_type is not None
        )

    @property
    def is_locked_by_conversion(self):
        """True if this doc can no longer be edited/voided because it was converted."""
        return self.is_converted_source

    @property
    def title_text(self):
        return DOC_TYPE_TITLE[self.doc_type]

    @property
    def preview_number(self):
        """Shown while still draft — NOT guaranteed to be the final number."""
        if self.number:
            return self.number
        year = timezone.now().year
        seq = InvoiceCounter.peek_next(self.doc_type, year)
        return f'{DOC_TYPE_PREFIX[self.doc_type]}-{year}-{seq:04d}'

    # ── Payment-bridge helpers ────────────────────────────────────────────────
    @property
    def is_billable(self):
        """Only Invoices generate receivables; Proformas/Delivery Notes don't."""
        return self.doc_type == DocType.INVOICE

    @property
    def is_paid(self):
        return self.paid_at is not None

    @property
    def can_be_marked_paid(self):
        """Finalized billable invoices that aren't already paid or voided."""
        return (
            self.is_billable
            and self.is_finalized
            and not self.is_paid
            and not self.is_voided
        )

    @property
    def payment_status_label(self):
        """Human-readable payment state for the UI."""
        if self.is_voided:
            return 'Voided'
        if not self.is_billable:
            return '—'
        if not self.is_finalized:
            return 'Draft'
        if self.is_paid:
            return 'Paid'
        # Finalized + unpaid — check overdue against the due_date
        if self.due_date and self.due_date < timezone.localdate():
            return 'Overdue'
        return 'Unpaid'

    @property
    def payment_status_color(self):
        return {
            'Paid':    '#10b981',
            'Unpaid':  '#f59e0b',
            'Overdue': '#ef4444',
            'Draft':   '#94a3b8',
            'Voided':  '#6b7280',
            '—':       '#cbd5e1',
        }.get(self.payment_status_label, '#94a3b8')

    # ── Calculation / numbering ───────────────────────────────────────────────
    def recalculate_totals(self, save=True):
        """Recompute subtotal, tax, total from line items + current tax/discount."""
        sub = sum((li.line_total for li in self.items.all()), Decimal('0.00'))
        tax = (sub * self.tax_rate / Decimal('100')).quantize(Decimal('0.01'))
        tot = (sub + tax - self.discount_amount).quantize(Decimal('0.01'))
        self.subtotal   = sub.quantize(Decimal('0.01'))
        self.tax_amount = tax
        self.total      = tot
        if save:
            self.save(update_fields=['subtotal', 'tax_amount', 'total', 'updated_at'])
        return self.total

    def allocate_number(self):
        """Allocates and assigns the next number. Called on finalize. Idempotent."""
        if self.number:
            return self.number
        year = timezone.now().year
        seq  = InvoiceCounter.allocate_next(self.doc_type, year)
        self.sequence = seq
        self.year     = year
        self.number   = f'{DOC_TYPE_PREFIX[self.doc_type]}-{year}-{seq:04d}'
        return self.number


# ── Line items ───────────────────────────────────────────────────────────────

class InvoiceLineItem(models.Model):
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice     = models.ForeignKey(InvoiceDocument, on_delete=models.CASCADE, related_name='items')
    position    = models.PositiveIntegerField(default=0)
    description = models.CharField(max_length=500)
    quantity    = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('1.00'))
    unit_price  = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    line_total  = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))

    class Meta:
        ordering = ['position', 'id']

    def __str__(self):
        return f'{self.description[:40]} — {self.line_total}'

    def save(self, *args, **kwargs):
        # Always recompute line total
        self.line_total = (Decimal(self.quantity) * Decimal(self.unit_price)).quantize(Decimal('0.01'))
        super().save(*args, **kwargs)


# ── Template ─────────────────────────────────────────────────────────────────

class InvoiceTemplate(models.Model):
    """
    A reusable invoice blueprint: letterhead + saved layout + default values.
    Can be locked so users who create invoices from it cannot drag blocks or
    change the doc type.
    """
    class Visibility(models.TextChoices):
        PERSONAL = 'personal', 'Personal (only me)'
        SHARED   = 'shared',   'Shared (all invoice users)'

    id             = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name           = models.CharField(max_length=150)
    description    = models.CharField(max_length=300, blank=True)

    letterhead     = models.ForeignKey(
        SharedFile, on_delete=models.PROTECT,
        related_name='invoice_templates',
    )
    layout_json    = models.JSONField(default=dict, blank=True)
    locked_layout  = models.BooleanField(
        default=False,
        help_text='If true, users cannot drag blocks when creating invoices from this template.',
    )
    # Optional locked doc type — NULL means the user can switch when using the template.
    doc_type       = models.CharField(
        max_length=20, choices=DocType.choices, blank=True, default='',
    )

    # Defaults pre-filled into new invoices created from this template
    default_currency      = models.CharField(max_length=3, default='GMD')
    default_tax_rate      = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('0.00'))
    default_payment_terms = models.CharField(max_length=200, blank=True, default='Net 30')
    default_bank_details  = models.TextField(blank=True)
    default_notes         = models.TextField(blank=True)

    visibility     = models.CharField(max_length=20, choices=Visibility.choices,
                                       default=Visibility.PERSONAL)
    owner          = models.ForeignKey(User, on_delete=models.CASCADE,
                                        related_name='owned_invoice_templates')
    usage_count    = models.PositiveIntegerField(default=0)
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['visibility', 'owner']),
        ]

    def __str__(self):
        return self.name

    def user_can_view(self, user):
        if self.visibility == self.Visibility.SHARED:
            return True
        return self.owner_id == user.id or user.is_superuser

    def user_can_edit(self, user):
        return self.owner_id == user.id or user.is_superuser

    @property
    def is_locked(self):
        return self.locked_layout

    @property
    def locks_doc_type(self):
        return bool(self.doc_type)