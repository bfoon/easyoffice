from django.contrib import admin
from django.db.models import Count
from apps.projects.models import (
    Project,
    Milestone,
    ProjectUpdate,
    Risk,
    Survey,
    SurveyQuestion,
    SurveyResponse,
    SurveyAnswer,
    TrackingSheet,
    TrackingRow,
    ProjectLocation,
)


# ─────────────────────────────────────────────────────────────────────────────
# Inlines
# ─────────────────────────────────────────────────────────────────────────────

class MilestoneInline(admin.TabularInline):
    model = Milestone
    extra = 0
    fields = ('name', 'due_date', 'status', 'order')
    ordering = ('order', 'due_date')
    show_change_link = True


class ProjectUpdateInline(admin.TabularInline):
    model = ProjectUpdate
    extra = 0
    fields = ('title', 'author', 'status_at_time', 'progress_at_time', 'created_at')
    readonly_fields = ('created_at',)
    show_change_link = True


class RiskInline(admin.TabularInline):
    model = Risk
    extra = 0
    fields = ('title', 'level', 'owner', 'is_resolved', 'created_at')
    readonly_fields = ('created_at',)
    show_change_link = True


class SurveyQuestionInline(admin.TabularInline):
    model = SurveyQuestion
    extra = 0
    fields = ('order', 'text', 'q_type', 'is_required')
    ordering = ('order',)
    show_change_link = True


class SurveyAnswerInline(admin.TabularInline):
    model = SurveyAnswer
    extra = 0
    fields = ('question', 'value')
    readonly_fields = ('question', 'value')
    show_change_link = False


class TrackingRowInline(admin.TabularInline):
    model = TrackingRow
    extra = 0
    fields = ('order', 'created_by', 'created_at')
    readonly_fields = ('created_at',)
    show_change_link = True


# ─────────────────────────────────────────────────────────────────────────────
# Project Admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'code',
        'project_manager',
        'department',
        'status',
        'priority',
        'progress_pct',
        'budget',
        'start_date',
        'end_date',
        'is_public',
        'created_at',
    )
    list_filter = (
        'status',
        'priority',
        'is_public',
        'department',
        'created_at',
        'start_date',
        'end_date',
    )
    search_fields = (
        'name',
        'code',
        'description',
        'tags',
        'project_manager__first_name',
        'project_manager__last_name',
        'project_manager__username',
    )
    filter_horizontal = ('team_members', 'units')
    readonly_fields = ('id', 'created_at', 'updated_at', 'completed_at', 'budget_spent')
    inlines = [MilestoneInline, ProjectUpdateInline, RiskInline]
    ordering = ('-created_at',)

    fieldsets = (
        ('Basic Information', {
            'fields': (
                'id',
                'name',
                'code',
                'description',
                'department',
                'units',
                'project_manager',
                'team_members',
            )
        }),
        ('Planning & Status', {
            'fields': (
                'status',
                'priority',
                'progress_pct',
                'start_date',
                'end_date',
                'completed_at',
            )
        }),
        ('Budget & Visibility', {
            'fields': (
                'budget',
                'budget_spent',
                'is_public',
                'tags',
                'cover_color',
            )
        }),
        ('Audit', {
            'fields': (
                'created_at',
                'updated_at',
            )
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('project_manager', 'department').prefetch_related('team_members', 'units')


# ─────────────────────────────────────────────────────────────────────────────
# Milestone Admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(Milestone)
class MilestoneAdmin(admin.ModelAdmin):
    list_display = ('name', 'project', 'due_date', 'status', 'order', 'completed_at')
    list_filter = ('status', 'due_date', 'project')
    search_fields = ('name', 'description', 'project__name', 'project__code')
    readonly_fields = ('id', 'completed_at')
    ordering = ('project', 'order', 'due_date')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('project')


# ─────────────────────────────────────────────────────────────────────────────
# Project Update Admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(ProjectUpdate)
class ProjectUpdateAdmin(admin.ModelAdmin):
    list_display = (
        'project',
        'title',
        'author',
        'status_at_time',
        'progress_at_time',
        'is_milestone_update',
        'created_at',
    )
    list_filter = ('is_milestone_update', 'status_at_time', 'created_at')
    search_fields = ('title', 'content', 'project__name', 'project__code', 'author__first_name', 'author__last_name')
    readonly_fields = ('id', 'created_at')
    ordering = ('-created_at',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('project', 'author', 'milestone')


# ─────────────────────────────────────────────────────────────────────────────
# Risk Admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(Risk)
class RiskAdmin(admin.ModelAdmin):
    list_display = ('title', 'project', 'level', 'owner', 'is_resolved', 'created_at')
    list_filter = ('level', 'is_resolved', 'created_at')
    search_fields = ('title', 'description', 'mitigation', 'project__name', 'project__code')
    readonly_fields = ('id', 'created_at')
    ordering = ('-created_at',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('project', 'owner')


# ─────────────────────────────────────────────────────────────────────────────
# Survey Admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(Survey)
class SurveyAdmin(admin.ModelAdmin):
    list_display = (
        'title',
        'project',
        'created_by',
        'is_active',
        'is_anonymous',
        'is_public',
        'allow_multiple_responses',
        'response_count_admin',
        'closes_at',
        'created_at',
    )
    list_filter = (
        'is_active',
        'is_anonymous',
        'is_public',
        'allow_multiple_responses',
        'created_at',
        'closes_at',
    )
    search_fields = (
        'title',
        'description',
        'project__name',
        'project__code',
        'created_by__first_name',
        'created_by__last_name',
    )
    readonly_fields = ('id', 'created_at', 'public_token', 'public_link')
    inlines = [SurveyQuestionInline]
    ordering = ('-created_at',)

    fieldsets = (
        ('Survey', {
            'fields': (
                'id',
                'project',
                'title',
                'description',
                'created_by',
            )
        }),
        ('Settings', {
            'fields': (
                'is_active',
                'is_anonymous',
                'is_public',
                'allow_multiple_responses',
                'closes_at',
            )
        }),
        ('Public Access', {
            'fields': (
                'public_token',
                'public_link',
            )
        }),
        ('Audit', {
            'fields': (
                'created_at',
            )
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('project', 'created_by').annotate(_resp_count=Count('responses'))

    @admin.display(description='Responses')
    def response_count_admin(self, obj):
        return getattr(obj, '_resp_count', obj.responses.count())

    @admin.display(description='Public link')
    def public_link(self, obj):
        try:
            return obj.get_public_url() if obj.public_token else '-'
        except Exception:
            return '-'


# ─────────────────────────────────────────────────────────────────────────────
# Survey Question Admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(SurveyQuestion)
class SurveyQuestionAdmin(admin.ModelAdmin):
    list_display = ('survey', 'order', 'text_short', 'q_type', 'is_required')
    list_filter = ('q_type', 'is_required', 'survey__project')
    search_fields = ('text', 'options', 'survey__title', 'survey__project__name', 'survey__project__code')
    readonly_fields = ('id',)
    ordering = ('survey', 'order')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('survey', 'survey__project')

    @admin.display(description='Question')
    def text_short(self, obj):
        return obj.text[:80]


# ─────────────────────────────────────────────────────────────────────────────
# Survey Response Admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(SurveyResponse)
class SurveyResponseAdmin(admin.ModelAdmin):
    list_display = (
        'survey',
        'respondent_display',
        'is_public_response',
        'ip_address',
        'device_id_short',
        'submitted_at',
    )
    list_filter = ('is_public_response', 'submitted_at', 'survey__project')
    search_fields = (
        'survey__title',
        'survey__project__name',
        'survey__project__code',
        'respondent_name',
        'respondent__first_name',
        'respondent__last_name',
        'ip_address',
        'device_id',
    )
    readonly_fields = (
        'id',
        'submitted_at',
        'ip_address',
        'device_id',
        'user_agent',
    )
    inlines = [SurveyAnswerInline]
    ordering = ('-submitted_at',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('survey', 'survey__project', 'respondent')

    @admin.display(description='Respondent')
    def respondent_display(self, obj):
        if obj.respondent:
            full_name = getattr(obj.respondent, 'full_name', None)
            if full_name:
                return full_name
            return str(obj.respondent)
        return obj.respondent_name or 'Anonymous/Public'

    @admin.display(description='Device ID')
    def device_id_short(self, obj):
        return (obj.device_id[:16] + '...') if obj.device_id and len(obj.device_id) > 16 else (obj.device_id or '-')


# ─────────────────────────────────────────────────────────────────────────────
# Survey Answer Admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(SurveyAnswer)
class SurveyAnswerAdmin(admin.ModelAdmin):
    list_display = ('response', 'question', 'value_short')
    list_filter = ('question__q_type', 'question__survey')
    search_fields = (
        'value',
        'question__text',
        'question__survey__title',
        'question__survey__project__name',
    )
    readonly_fields = ('id',)
    ordering = ('response',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('response', 'question', 'question__survey')

    @admin.display(description='Answer')
    def value_short(self, obj):
        return obj.value[:100]


# ─────────────────────────────────────────────────────────────────────────────
# Tracking Sheet Admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(TrackingSheet)
class TrackingSheetAdmin(admin.ModelAdmin):
    list_display = ('title', 'project', 'created_by', 'column_count', 'row_count', 'created_at', 'updated_at')
    list_filter = ('created_at', 'updated_at')
    search_fields = ('title', 'project__name', 'project__code', 'created_by__first_name', 'created_by__last_name')
    readonly_fields = ('id', 'created_at', 'updated_at', 'columns_preview')
    inlines = [TrackingRowInline]
    ordering = ('-created_at',)

    fieldsets = (
        ('Tracking Sheet', {
            'fields': (
                'id',
                'project',
                'title',
                'created_by',
            )
        }),
        ('Columns', {
            'fields': (
                'columns_json',
                'columns_preview',
            )
        }),
        ('Audit', {
            'fields': (
                'created_at',
                'updated_at',
            )
        }),
    )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('project', 'created_by').annotate(_row_count=Count('rows'))

    @admin.display(description='Columns')
    def column_count(self, obj):
        try:
            return len(obj.get_columns())
        except Exception:
            return 0

    @admin.display(description='Rows')
    def row_count(self, obj):
        return getattr(obj, '_row_count', obj.rows.count())

    @admin.display(description='Columns preview')
    def columns_preview(self, obj):
        try:
            cols = obj.get_columns()
            if not cols:
                return '-'
            return ', '.join([f"{c.get('label')} ({c.get('type')})" for c in cols])
        except Exception:
            return '-'


# ─────────────────────────────────────────────────────────────────────────────
# Tracking Row Admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(TrackingRow)
class TrackingRowAdmin(admin.ModelAdmin):
    list_display = ('sheet', 'order', 'created_by', 'created_at', 'data_preview')
    list_filter = ('created_at', 'sheet__project')
    search_fields = ('sheet__title', 'sheet__project__name', 'sheet__project__code', 'data_json')
    readonly_fields = ('id', 'created_at', 'data_preview')
    ordering = ('sheet', 'order', 'created_at')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('sheet', 'sheet__project', 'created_by')

    @admin.display(description='Data')
    def data_preview(self, obj):
        try:
            data = obj.get_data()
            if not data:
                return '-'
            preview = ', '.join([f'{k}: {v}' for k, v in list(data.items())[:4]])
            return preview[:140]
        except Exception:
            return obj.data_json[:140]


# ─────────────────────────────────────────────────────────────────────────────
# Project Location Admin
# ─────────────────────────────────────────────────────────────────────────────

@admin.register(ProjectLocation)
class ProjectLocationAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'project',
        'category',
        'status',
        'assigned_to',
        'latitude',
        'longitude',
        'created_at',
    )
    list_filter = ('status', 'category', 'created_at', 'project')
    search_fields = (
        'name',
        'description',
        'address',
        'category',
        'notes',
        'project__name',
        'project__code',
        'assigned_to__first_name',
        'assigned_to__last_name',
    )
    readonly_fields = ('id', 'created_at')
    ordering = ('project', 'name')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('project', 'assigned_to')