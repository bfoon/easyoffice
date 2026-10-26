from django.contrib import admin

from apps.investments import models as m


@admin.register(m.InvestorInvitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ('email', 'full_name', 'status', 'expires_at', 'sent_count', 'invited_by')
    list_filter = ('status',)
    search_fields = ('email', 'full_name')
    readonly_fields = ('token',)


@admin.register(m.InvestorApplication)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'email', 'status', 'created_at', 'reviewed_by')
    list_filter = ('status', 'risk_tolerance')
    search_fields = ('full_name', 'email', 'id_number', 'phone')
    exclude = ('id_document', 'proof_of_address')


@admin.register(m.InvestorAccount)
class AccountAdmin(admin.ModelAdmin):
    list_display = ('account_no', 'user', 'status', 'wallet_balance', 'portal_only', 'created_at')
    list_filter = ('status', 'portal_only')
    search_fields = ('account_no', 'user__email', 'user__first_name', 'user__last_name')
    readonly_fields = ('wallet_balance', 'activation_token')


class ReadOnlyLedger(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(m.WalletTransaction)
class WalletTxnAdmin(ReadOnlyLedger):
    list_display = ('created_at', 'account', 'kind', 'amount', 'balance_after', 'reference')
    list_filter = ('kind',)
    search_fields = ('account__account_no', 'reference', 'description')


@admin.register(m.HoldingEvent)
class HoldingEventAdmin(ReadOnlyLedger):
    list_display = ('occurred_at', 'holding', 'kind', 'amount', 'capital_after')
    list_filter = ('kind',)


@admin.register(m.InvestmentOpportunity)
class OpportunityAdmin(admin.ModelAdmin):
    list_display = ('title', 'status', 'risk_level', 'investor_share_pct', 'target_capital', 'distribution_frequency')
    list_filter = ('status', 'risk_level')
    filter_horizontal = ('products',)
    prepopulated_fields = {'slug': ('title',)}


@admin.register(m.Holding)
class HoldingAdmin(admin.ModelAdmin):
    list_display = ('account', 'opportunity', 'capital', 'profit_total', 'payout_mode', 'status')
    list_filter = ('status', 'payout_mode')
    readonly_fields = [f.name for f in m.Holding._meta.fields]


@admin.register(m.Distribution)
class DistributionAdmin(admin.ModelAdmin):
    list_display = ('opportunity', 'period_start', 'status', 'net_profit', 'investor_pool', 'yield_pct')
    list_filter = ('status', 'opportunity')
    readonly_fields = [f.name for f in m.Distribution._meta.fields]


admin.site.register(m.DepositRequest)
admin.site.register(m.WithdrawalRequest)
admin.site.register(m.OpportunityUpdate)
admin.site.register(m.ProfitAdjustment)
