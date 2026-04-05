from django.contrib import admin
from apps.finance.models import Budget, PurchaseRequest, Payment


@admin.register(Budget)
class BudgetAdmin(admin.ModelAdmin):
    list_display = ['name', 'fiscal_year', 'department', 'total_amount', 'spent_amount', 'status']
    list_filter = ['fiscal_year', 'status', 'department']


@admin.register(PurchaseRequest)
class PurchaseRequestAdmin(admin.ModelAdmin):
    list_display = ['title', 'requested_by', 'department', 'estimated_cost', 'priority', 'status', 'created_at']
    list_filter = ['status', 'priority', 'department']


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ['reference', 'description', 'amount', 'method', 'paid_by', 'payment_date']
    list_filter = ['method', 'payment_date']
