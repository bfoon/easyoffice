from django.contrib import admin
from .models import OpportunitySource, OpportunityKeyword, OpportunityWatcher, OpportunityMatch


@admin.register(OpportunitySource)
class OpportunitySourceAdmin(admin.ModelAdmin):
    list_display = ('name', 'source_type', 'url', 'is_active', 'last_checked_at', 'last_status')
    list_filter = ('source_type', 'is_active', 'last_status')
    search_fields = ('name', 'url')


@admin.register(OpportunityKeyword)
class OpportunityKeywordAdmin(admin.ModelAdmin):
    list_display = ('keyword', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('keyword',)


@admin.register(OpportunityWatcher)
class OpportunityWatcherAdmin(admin.ModelAdmin):
    list_display = ('user', 'source', 'notify_email', 'notify_in_app', 'is_active')
    list_filter = ('notify_email', 'notify_in_app', 'is_active', 'source')
    search_fields = ('user__first_name', 'user__last_name', 'user__email', 'source__name')


@admin.register(OpportunityMatch)
class OpportunityMatchAdmin(admin.ModelAdmin):
    list_display = (
        'title', 'reference_no', 'country', 'procurement_process',
        'source', 'posted_date', 'deadline', 'is_read', 'is_task_created'
    )
    list_filter = ('source', 'country', 'keyword', 'is_read', 'is_task_created')
    search_fields = ('title', 'reference_no', 'country', 'summary', 'external_url')