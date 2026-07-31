"""
apps/finance/admin.py
=====================

Admin for the whole finance app.

The previous version registered 3 of the app's 24 models, so Contracts,
invoices, payment requests, loans, the audit trail, anomalies, maintenance
rosters and sales targets were all invisible in /admin/ — which is exactly
where you need them when something goes wrong in production.

Conventions used throughout:
  * raw_id_fields for every User / Project / Budget FK — a plain FK renders a
    <select> containing EVERY user on the system on each page load.
  * list_select_related to kill the N+1 queries the changelist would otherwise
    fire for each row's FK.
  * auto_now / auto_now_add / editable=False fields are readonly, never form
    fields (Django would reject them otherwise).
  * FinanceAuditLog is append-only: no add, no change, no delete. An audit
    trail you can edit from the admin is not an audit trail.
"""
from django.contrib import admin
from django.utils.html import format_html

from apps.finance.models import (
    Budget,
    Contract,
    ContractAlertLog,
    ContractDocument,
    ContractExtension,
    ContractInvoiceLink,
    ContractSignature,
    ContractSignatureRequest,
    EmployeeFinanceRequest,
    EmployeeLoan,
    EmployeeLoanPayment,
    FinanceAnomaly,
    FinanceAuditLog,
    IncomingPaymentDocument,
    IncomingPaymentRequest,
    MaintenanceRoster,
    MaintenanceRosterMember,
    MaintenanceVisit,
    Payment,
    PaymentRequest,
    PaymentRequestDocument,
    PurchaseRequest,
)
from apps.finance.models_sales_targets import SalesRewardPayout, SalesTarget


# ─── Shared helpers ──────────────────────────────────────────────────────────

class ReadOnlyAdminMixin:
    """Look, filter and search — but never add, edit or delete."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def _pill(text, color):
    return format_html(
        '<span style="display:inline-block;padding:2px 9px;border-radius:999px;'
        'background:{}1a;color:{};font-weight:600;font-size:11px">{}</span>',
        color, color, text,
    )


# ─── Budgets ─────────────────────────────────────────────────────────────────

@admin.register(Budget)
class BudgetAdmin(admin.ModelAdmin):
    list_display = [
        'name', 'fiscal_year', 'department', 'total_amount',
        'spent_amount', 'balance_display', 'utilization_display', 'status',
    ]
    list_filter = ['fiscal_year', 'status', 'department']
    search_fields = ['name']
    list_select_related = ['department', 'unit']
    raw_id_fields = ['approved_by']
    readonly_fields = ['id', 'created_at']
    ordering = ['-fiscal_year', 'name']
    list_per_page = 50

    @admin.display(description='Balance')
    def balance_display(self, obj):
        return f'{obj.balance:,.2f}'

    @admin.display(description='Used')
    def utilization_display(self, obj):
        pct = obj.utilization_pct
        color = '#ef4444' if pct >= 90 else '#f59e0b' if pct >= 70 else '#10b981'
        return _pill(f'{pct}%', color)


# ─── Purchase requests ───────────────────────────────────────────────────────

@admin.register(PurchaseRequest)
class PurchaseRequestAdmin(admin.ModelAdmin):
    list_display = [
        'title', 'requested_by', 'department', 'estimated_cost',
        'actual_cost', 'priority', 'status', 'created_at',
    ]
    list_filter = ['status', 'priority', 'department', 'created_at']
    search_fields = [
        'title', 'description', 'justification', 'vendor',
        'requested_by__username', 'requested_by__email',
    ]
    list_select_related = ['requested_by', 'department', 'budget']
    raw_id_fields = ['requested_by', 'budget', 'project', 'approved_by', 'processed_by']
    readonly_fields = ['id', 'created_at', 'updated_at']
    date_hierarchy = 'created_at'
    list_per_page = 50
    fieldsets = (
        ('Request', {
            'fields': ('title', 'description', 'justification', 'requested_by',
                       'department', 'vendor', 'attachment'),
        }),
        ('Money & linking', {
            'fields': ('estimated_cost', 'actual_cost', 'budget', 'project',
                       'expected_delivery'),
        }),
        ('Workflow', {
            'fields': ('priority', 'status', 'approved_by', 'approval_date',
                       'approval_notes', 'processed_by', 'processed_at', 'notes'),
        }),
        ('System', {
            'classes': ('collapse',),
            'fields': ('id', 'created_at', 'updated_at'),
        }),
    )


# ─── Employee finance requests & loans ───────────────────────────────────────

@admin.register(EmployeeFinanceRequest)
class EmployeeFinanceRequestAdmin(admin.ModelAdmin):
    list_display = [
        'title', 'employee', 'request_type', 'amount_requested',
        'amount_approved', 'status', 'created_at',
    ]
    list_filter = ['status', 'request_type', 'created_at']
    search_fields = ['title', 'reason', 'employee__username', 'employee__email']
    list_select_related = ['employee', 'budget']
    raw_id_fields = [
        'employee', 'project', 'budget', 'approved_by',
        'processed_by', 'linked_payment',
    ]
    readonly_fields = ['id', 'created_at', 'updated_at']
    date_hierarchy = 'created_at'
    list_per_page = 50


class EmployeeLoanPaymentInline(admin.TabularInline):
    model = EmployeeLoanPayment
    extra = 0
    fields = ['amount', 'source', 'payment_date', 'payment', 'created_at']
    readonly_fields = ['created_at']
    raw_id_fields = ['payment']


@admin.register(EmployeeLoan)
class EmployeeLoanAdmin(admin.ModelAdmin):
    list_display = [
        'employee', 'approved_amount', 'disbursed_amount', 'amount_repaid',
        'balance_display', 'repayment_months', 'monthly_payment',
        'status', 'created_at',
    ]
    list_filter = ['status', 'created_at']
    search_fields = ['employee__username', 'employee__email']
    list_select_related = ['employee']
    raw_id_fields = ['employee', 'source_request', 'approved_by']
    readonly_fields = ['id', 'created_at', 'updated_at']
    inlines = [EmployeeLoanPaymentInline]
    date_hierarchy = 'created_at'

    @admin.display(description='Outstanding')
    def balance_display(self, obj):
        return f'{obj.balance:,.2f}'


@admin.register(EmployeeLoanPayment)
class EmployeeLoanPaymentAdmin(admin.ModelAdmin):
    list_display = ['loan', 'amount', 'source', 'payment_date', 'payment']
    list_filter = ['source', 'payment_date']
    search_fields = ['loan__employee__username', 'loan__employee__email']
    list_select_related = ['loan', 'loan__employee', 'payment']
    raw_id_fields = ['loan', 'payment']
    readonly_fields = ['id', 'created_at']
    date_hierarchy = 'payment_date'


# ─── Payments ────────────────────────────────────────────────────────────────

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = [
        'reference', 'description_short', 'amount_display', 'method',
        'payment_type', 'direction', 'recipient', 'paid_by', 'payment_date',
    ]
    list_filter = ['method', 'payment_type', 'direction', 'currency', 'payment_date']
    search_fields = ['reference', 'description', 'recipient', 'notes']
    list_select_related = ['paid_by', 'employee', 'budget']
    raw_id_fields = [
        'purchase_request', 'employee_request', 'loan', 'paid_by',
        'processed_by', 'approved_by', 'employee', 'budget', 'project',
    ]
    readonly_fields = ['id', 'created_at']
    date_hierarchy = 'payment_date'
    list_per_page = 50

    @admin.display(description='Description')
    def description_short(self, obj):
        text = obj.description or ''
        return (text[:60] + '…') if len(text) > 60 else text

    @admin.display(description='Amount', ordering='amount')
    def amount_display(self, obj):
        return f'{obj.currency} {obj.amount:,.2f}'


# ─── Outgoing payment requests ───────────────────────────────────────────────

class PaymentRequestDocumentInline(admin.TabularInline):
    model = PaymentRequestDocument
    extra = 0
    fields = ['doc_type', 'name', 'file', 'uploaded_by', 'uploaded_at']
    readonly_fields = ['uploaded_at']
    raw_id_fields = ['uploaded_by']


@admin.register(PaymentRequest)
class PaymentRequestAdmin(admin.ModelAdmin):
    list_display = [
        'title', 'recipient_display', 'recipient_type', 'amount_display',
        'method', 'status', 'due_date', 'created_at',
    ]
    list_filter = ['status', 'recipient_type', 'method', 'payment_type',
                   'currency', 'created_at']
    search_fields = [
        'title', 'description', 'recipient_name', 'recipient_email',
        'requested_by__username', 'requested_by__email',
    ]
    list_select_related = ['recipient_user', 'requested_by', 'linked_payment']
    raw_id_fields = [
        'recipient_user', 'budget', 'project', 'purchase_request',
        'requested_by', 'approved_by', 'linked_payment',
    ]
    readonly_fields = ['id', 'created_at', 'updated_at']
    inlines = [PaymentRequestDocumentInline]
    date_hierarchy = 'created_at'
    list_per_page = 50

    @admin.display(description='Recipient')
    def recipient_display(self, obj):
        return obj.effective_recipient_name

    @admin.display(description='Amount', ordering='amount')
    def amount_display(self, obj):
        return f'{obj.currency} {obj.amount:,.2f}'


@admin.register(PaymentRequestDocument)
class PaymentRequestDocumentAdmin(admin.ModelAdmin):
    list_display = ['name', 'doc_type', 'payment_request', 'uploaded_by', 'uploaded_at']
    list_filter = ['doc_type', 'uploaded_at']
    search_fields = ['name', 'payment_request__title']
    list_select_related = ['payment_request', 'uploaded_by']
    raw_id_fields = ['payment_request', 'uploaded_by']
    readonly_fields = ['id', 'uploaded_at']


# ─── Incoming payment requests (customer invoices) ───────────────────────────

class IncomingPaymentDocumentInline(admin.TabularInline):
    model = IncomingPaymentDocument
    extra = 0
    fields = ['doc_type', 'name', 'file', 'uploaded_by', 'uploaded_at']
    readonly_fields = ['uploaded_at']
    raw_id_fields = ['uploaded_by']


@admin.register(IncomingPaymentRequest)
class IncomingPaymentRequestAdmin(admin.ModelAdmin):
    list_display = [
        'invoice_number', 'customer_name', 'customer_company', 'total_display',
        'status', 'issue_date', 'due_date', 'overdue_display', 'created_by',
    ]
    list_filter = ['status', 'currency', 'issue_date', 'due_date', 'created_at']
    search_fields = [
        'invoice_number', 'title', 'description', 'customer_name',
        'customer_company', 'customer_email', 'customer_phone', 'notes',
    ]
    list_select_related = ['created_by', 'project', 'budget']
    raw_id_fields = ['project', 'budget', 'created_by']
    readonly_fields = [
        'id', 'created_at', 'updated_at', 'last_reminder_sent',
        'reminder_count', 'total_display',
    ]
    inlines = [IncomingPaymentDocumentInline]
    date_hierarchy = 'issue_date'
    list_per_page = 50
    fieldsets = (
        ('Invoice', {
            'fields': ('invoice_number', 'title', 'description', 'status'),
        }),
        ('Customer', {
            'fields': ('customer_name', 'customer_company', 'customer_email',
                       'customer_phone', 'customer_address'),
        }),
        ('Money', {
            'fields': ('amount', 'tax_amount', 'discount_amount', 'currency',
                       'total_display'),
        }),
        ('Dates', {
            'fields': ('issue_date', 'due_date', 'paid_at'),
        }),
        ('Linking & notes', {
            'fields': ('project', 'budget', 'payment_instructions', 'notes',
                       'created_by'),
        }),
        ('Reminders', {
            'classes': ('collapse',),
            'fields': ('last_reminder_sent', 'reminder_count'),
        }),
        ('System', {
            'classes': ('collapse',),
            'fields': ('id', 'created_at', 'updated_at'),
        }),
    )

    @admin.display(description='Total')
    def total_display(self, obj):
        return f'{obj.currency} {obj.total_amount:,.2f}'

    @admin.display(description='Overdue', boolean=True)
    def overdue_display(self, obj):
        return obj.is_overdue


@admin.register(IncomingPaymentDocument)
class IncomingPaymentDocumentAdmin(admin.ModelAdmin):
    list_display = ['name', 'doc_type', 'payment_request', 'uploaded_by', 'uploaded_at']
    list_filter = ['doc_type', 'uploaded_at']
    search_fields = ['name', 'payment_request__invoice_number',
                     'payment_request__customer_name']
    list_select_related = ['payment_request', 'uploaded_by']
    raw_id_fields = ['payment_request', 'uploaded_by']
    readonly_fields = ['id', 'uploaded_at']


# ─── Contracts ───────────────────────────────────────────────────────────────

class ContractExtensionInline(admin.TabularInline):
    model = ContractExtension
    extra = 0
    fields = ['extension_number', 'reference', 'previous_end_date',
              'new_end_date', 'additional_amount', 'effective_date']
    readonly_fields = ['reference']
    ordering = ['extension_number']


class ContractDocumentInline(admin.TabularInline):
    model = ContractDocument
    extra = 0
    fields = ['source', 'title', 'version', 'docx_file', 'pdf_file',
              'generated_by', 'created_at']
    readonly_fields = ['created_at']
    raw_id_fields = ['generated_by']


class MaintenanceRosterInline(admin.TabularInline):
    model = MaintenanceRoster
    extra = 0
    fields = ['title', 'frequency', 'start_date', 'end_date',
              'next_visit_date', 'active']
    show_change_link = True


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = [
        'title', 'reference', 'contract_type', 'counterparty_display',
        'start_date', 'end_date', 'status', 'billing_cycle',
        'standard_cost', 'auto_generate_invoice', 'next_invoice_date',
    ]
    list_filter = [
        'status', 'contract_type', 'billing_cycle', 'auto_generate_invoice',
        'auto_send_invoice', 'currency', 'end_date',
    ]
    search_fields = [
        'title', 'reference', 'description', 'vendor_name', 'vendor_company',
        'vendor_email', 'staff__username', 'staff__email',
    ]
    list_select_related = ['staff', 'project', 'budget']
    raw_id_fields = [
        'staff', 'project', 'budget', 'default_invoice_template',
        'created_by', 'updated_by',
    ]
    readonly_fields = ['id', 'created_at', 'updated_at', 'days_to_end_display']
    inlines = [ContractExtensionInline, ContractDocumentInline, MaintenanceRosterInline]
    date_hierarchy = 'end_date'
    list_per_page = 50
    fieldsets = (
        ('Contract', {
            'fields': ('title', 'contract_type', 'reference', 'description',
                       'status', 'document'),
        }),
        ('Counterparty', {
            'fields': ('staff', 'vendor_name', 'vendor_company', 'vendor_email',
                       'vendor_phone', 'vendor_address'),
        }),
        ('Dates', {
            'fields': ('start_date', 'end_date', 'renewal_date',
                       'days_to_end_display'),
        }),
        ('Billing', {
            'fields': ('standard_cost', 'currency', 'billing_cycle',
                       'auto_generate_invoice', 'auto_send_invoice',
                       'next_invoice_date', 'last_invoice_date',
                       'default_invoice_template'),
        }),
        ('Alerts', {
            'fields': ('alert_days_before_end', 'alert_days_before_renewal',
                       'send_expiry_alerts', 'send_renewal_alerts'),
        }),
        ('Linking & ownership', {
            'fields': ('project', 'budget', 'created_by', 'updated_by'),
        }),
        ('System', {
            'classes': ('collapse',),
            'fields': ('id', 'created_at', 'updated_at'),
        }),
    )

    @admin.display(description='Counterparty')
    def counterparty_display(self, obj):
        return obj.counterparty_name

    @admin.display(description='Days to end')
    def days_to_end_display(self, obj):
        if not obj.pk:
            return '—'
        days = obj.days_to_end
        color = '#ef4444' if days < 0 else '#f59e0b' if days <= 30 else '#10b981'
        label = f'{days} days' if days >= 0 else f'{abs(days)} days ago'
        return _pill(label, color)


@admin.register(ContractExtension)
class ContractExtensionAdmin(admin.ModelAdmin):
    list_display = [
        'reference', 'contract', 'extension_number', 'previous_end_date',
        'new_end_date', 'days_added_display', 'additional_amount', 'effective_date',
    ]
    list_filter = ['effective_date', 'new_end_date']
    search_fields = ['reference', 'contract__title', 'contract__reference', 'reason']
    list_select_related = ['contract']
    raw_id_fields = ['contract', 'created_by']
    readonly_fields = ['id', 'reference', 'created_at', 'updated_at']

    @admin.display(description='Days added')
    def days_added_display(self, obj):
        return obj.days_added


@admin.register(ContractAlertLog)
class ContractAlertLogAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['contract', 'alert_type', 'sent_to', 'sent_at', 'sent_by']
    list_filter = ['alert_type', 'sent_at']
    search_fields = ['contract__title', 'sent_to', 'message']
    list_select_related = ['contract', 'sent_by']
    date_hierarchy = 'sent_at'


@admin.register(ContractDocument)
class ContractDocumentAdmin(admin.ModelAdmin):
    list_display = ['display_name_display', 'contract', 'source', 'version',
                    'has_pdf_display', 'has_docx_display', 'created_at']
    list_filter = ['source', 'created_at']
    search_fields = ['title', 'contract__title', 'contract__reference']
    list_select_related = ['contract', 'generated_by']
    raw_id_fields = ['contract', 'generated_by']
    readonly_fields = ['id', 'created_at', 'updated_at', 'snapshot']
    date_hierarchy = 'created_at'

    @admin.display(description='Document')
    def display_name_display(self, obj):
        return obj.display_name

    @admin.display(description='PDF', boolean=True)
    def has_pdf_display(self, obj):
        return obj.has_pdf

    @admin.display(description='DOCX', boolean=True)
    def has_docx_display(self, obj):
        return obj.has_docx


class ContractSignatureInline(admin.StackedInline):
    model = ContractSignature
    extra = 0
    can_delete = False
    readonly_fields = [
        'signer_name_at_signing', 'signer_email_at_signing', 'method',
        'signature_image', 'typed_text', 'consent_text', 'consent_given',
        'signed_ip', 'signed_user_agent', 'document_hash_at_signing',
        'created_at',
    ]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ContractSignatureRequest)
class ContractSignatureRequestAdmin(admin.ModelAdmin):
    list_display = [
        'contract', 'signer_name', 'signer_email', 'status',
        'sent_at', 'signed_at', 'expires_at', 'expired_display',
    ]
    list_filter = ['status', 'created_at', 'expires_at']
    search_fields = ['signer_name', 'signer_email', 'contract__title',
                     'contract__reference']
    list_select_related = ['contract', 'document']
    raw_id_fields = ['contract', 'document', 'created_by', 'voided_by']
    readonly_fields = [
        'id', 'access_token', 'document_hash', 'sent_at', 'first_viewed_at',
        'last_viewed_at', 'signed_at', 'declined_at', 'voided_at',
        'created_at', 'updated_at',
    ]
    inlines = [ContractSignatureInline]
    date_hierarchy = 'created_at'

    @admin.display(description='Expired', boolean=True)
    def expired_display(self, obj):
        return bool(obj.is_expired)


@admin.register(ContractSignature)
class ContractSignatureAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ['signer_name_at_signing', 'signer_email_at_signing',
                    'method', 'consent_given', 'signed_ip', 'created_at']
    list_filter = ['method', 'consent_given', 'created_at']
    search_fields = ['signer_name_at_signing', 'signer_email_at_signing']
    list_select_related = ['request', 'request__contract']
    date_hierarchy = 'created_at'


@admin.register(ContractInvoiceLink)
class ContractInvoiceLinkAdmin(admin.ModelAdmin):
    list_display = ['contract', 'invoice', 'source', 'period_start',
                    'period_end', 'generated_by', 'created_at']
    list_filter = ['source', 'created_at']
    search_fields = ['contract__title', 'contract__reference', 'invoice__number']
    list_select_related = ['contract', 'invoice', 'generated_by']
    raw_id_fields = ['contract', 'invoice', 'generated_by']
    readonly_fields = ['id', 'created_at']
    date_hierarchy = 'created_at'


# ─── Audit trail & anomalies ─────────────────────────────────────────────────

@admin.register(FinanceAuditLog)
class FinanceAuditLogAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Append-only. Deliberately not editable from the admin."""

    list_display = [
        'timestamp', 'actor_display', 'action', 'model_name',
        'object_repr', 'amount_display', 'ip_address',
    ]
    list_filter = ['action', 'model_name', 'timestamp']
    search_fields = ['actor_name', 'object_repr', 'object_id', 'notes',
                     'model_name']
    list_select_related = ['actor']
    date_hierarchy = 'timestamp'
    list_per_page = 100

    @admin.display(description='Actor')
    def actor_display(self, obj):
        return obj.actor_name or (obj.actor and str(obj.actor)) or 'System'

    @admin.display(description='Amount')
    def amount_display(self, obj):
        if obj.amount is None:
            return '—'
        return f'{obj.currency or ""} {obj.amount:,.2f}'.strip()


@admin.register(FinanceAnomaly)
class FinanceAnomalyAdmin(admin.ModelAdmin):
    list_display = ['title', 'kind', 'severity_display', 'status',
                    'model_name', 'amount', 'detected_at', 'reviewed_by']
    list_filter = ['status', 'severity', 'kind', 'detected_at']
    search_fields = ['title', 'description', 'model_name', 'object_id',
                     'resolution']
    list_select_related = ['reviewed_by', 'audit_entry']
    raw_id_fields = ['audit_entry', 'reviewed_by']
    readonly_fields = ['id', 'detected_at', 'extra']
    date_hierarchy = 'detected_at'
    list_per_page = 50
    actions = ['mark_reviewing', 'mark_dismissed']

    @admin.display(description='Severity', ordering='severity')
    def severity_display(self, obj):
        return _pill(obj.get_severity_display(), obj.severity_color)

    @admin.action(description='Mark selected anomalies as under review')
    def mark_reviewing(self, request, queryset):
        updated = queryset.update(status=FinanceAnomaly.Status.REVIEWING)
        self.message_user(request, f'{updated} anomaly(ies) marked under review.')

    @admin.action(description='Dismiss selected anomalies')
    def mark_dismissed(self, request, queryset):
        updated = queryset.update(status=FinanceAnomaly.Status.DISMISSED)
        self.message_user(request, f'{updated} anomaly(ies) dismissed.')


# ─── Maintenance rosters ─────────────────────────────────────────────────────

class MaintenanceRosterMemberInline(admin.TabularInline):
    model = MaintenanceRosterMember
    extra = 0
    fields = ['user', 'external_name', 'external_email', 'is_lead', 'added_at']
    readonly_fields = ['added_at']
    raw_id_fields = ['user']


@admin.register(MaintenanceRoster)
class MaintenanceRosterAdmin(admin.ModelAdmin):
    list_display = ['title', 'contract', 'frequency', 'start_date', 'end_date',
                    'next_visit_date', 'active', 'send_calendar_invites']
    list_filter = ['active', 'frequency', 'send_calendar_invites', 'create_task']
    search_fields = ['title', 'description', 'location', 'contract__title',
                     'contract__reference']
    list_select_related = ['contract']
    raw_id_fields = ['contract', 'created_by']
    readonly_fields = ['id', 'created_at', 'updated_at']
    inlines = [MaintenanceRosterMemberInline]


@admin.register(MaintenanceRosterMember)
class MaintenanceRosterMemberAdmin(admin.ModelAdmin):
    list_display = ['display_name_display', 'roster', 'is_lead', 'email_display',
                    'added_at']
    list_filter = ['is_lead', 'added_at']
    search_fields = ['external_name', 'external_email', 'user__username',
                     'user__email', 'roster__title']
    list_select_related = ['roster', 'user']
    raw_id_fields = ['roster', 'user']
    readonly_fields = ['id', 'added_at']

    @admin.display(description='Member')
    def display_name_display(self, obj):
        return obj.display_name

    @admin.display(description='Email')
    def email_display(self, obj):
        return obj.email or '—'


@admin.register(MaintenanceVisit)
class MaintenanceVisitAdmin(admin.ModelAdmin):
    list_display = ['roster', 'scheduled_date', 'status', 'outcome',
                    'completed_by_name', 'completed_at', 'overdue_display']
    list_filter = ['status', 'outcome', 'follow_up_required', 'scheduled_date']
    search_fields = ['roster__title', 'roster__contract__title',
                     'completed_by_name', 'work_done', 'issues_found']
    list_select_related = ['roster', 'roster__contract', 'completed_by_user']
    raw_id_fields = ['roster', 'task', 'completed_by_user']
    readonly_fields = [
        'id', 'access_token', 'ics_uid', 'ics_sequence', 'invites_sent_at',
        'created_at', 'updated_at',
    ]
    date_hierarchy = 'scheduled_date'
    list_per_page = 50

    @admin.display(description='Overdue', boolean=True)
    def overdue_display(self, obj):
        return obj.is_overdue


# ─── Sales targets & rewards ─────────────────────────────────────────────────

@admin.register(SalesTarget)
class SalesTargetAdmin(admin.ModelAdmin):
    list_display = ['scope_label_display', 'period_label_display', 'scope',
                    'target_amount', 'reward_amount', 'currency', 'basis',
                    'is_active']
    list_filter = ['is_active', 'scope', 'period_type', 'basis', 'year', 'currency']
    search_fields = ['notes', 'agent__username', 'agent__email']
    list_select_related = ['agent', 'set_by']
    raw_id_fields = ['agent', 'set_by']
    readonly_fields = ['id', 'created_at', 'updated_at']

    @admin.display(description='Target for')
    def scope_label_display(self, obj):
        return obj.scope_label

    @admin.display(description='Period')
    def period_label_display(self, obj):
        return obj.period_label


@admin.register(SalesRewardPayout)
class SalesRewardPayoutAdmin(admin.ModelAdmin):
    list_display = ['beneficiary_display', 'reward_amount', 'achieved_amount',
                    'currency', 'status', 'approved_by', 'paid_by', 'created_at']
    list_filter = ['status', 'currency', 'created_at']
    search_fields = ['payment_reference', 'notes']
    list_select_related = ['target', 'target__agent', 'approved_by', 'paid_by']
    raw_id_fields = ['target', 'approved_by', 'paid_by']
    readonly_fields = ['id', 'created_at', 'updated_at']
    date_hierarchy = 'created_at'

    @admin.display(description='Beneficiary')
    def beneficiary_display(self, obj):
        return obj.beneficiary_label