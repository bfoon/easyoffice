from django.contrib import admin

from .models import FormTemplate, FormSubmission, SubmissionStep


class SubmissionStepInline(admin.TabularInline):
    model = SubmissionStep
    extra = 0
    readonly_fields = ('label', 'action', 'assignee_type', 'assignee_value',
                       'assigned_user', 'status', 'acted_by', 'acted_at', 'comment')
    can_delete = False


@admin.register(FormTemplate)
class FormTemplateAdmin(admin.ModelAdmin):
    list_display = ('title', 'status', 'letterhead_mode', 'created_by', 'updated_at')
    list_filter = ('status', 'letterhead_mode')
    search_fields = ('title', 'description')


@admin.register(FormSubmission)
class FormSubmissionAdmin(admin.ModelAdmin):
    list_display = ('ref_no', 'template', 'submitted_by', 'status', 'submitted_at')
    list_filter = ('status',)
    search_fields = ('ref_no',)
    inlines = [SubmissionStepInline]
