from django.contrib import admin
from apps.projects.models import Project, Milestone, ProjectUpdate, Risk


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'project_manager', 'status', 'priority', 'progress_pct', 'start_date', 'end_date']
    list_filter = ['status', 'priority', 'department']
    search_fields = ['name', 'code']


@admin.register(Milestone)
class MilestoneAdmin(admin.ModelAdmin):
    list_display = ['name', 'project', 'due_date', 'status']
    list_filter = ['status']


@admin.register(ProjectUpdate)
class ProjectUpdateAdmin(admin.ModelAdmin):
    list_display = ['project', 'author', 'created_at']


@admin.register(Risk)
class RiskAdmin(admin.ModelAdmin):
    list_display = ['title', 'project', 'level', 'owner', 'is_resolved']
