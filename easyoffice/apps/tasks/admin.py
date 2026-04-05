from django.contrib import admin
from apps.tasks.models import Task, TaskCategory, TaskComment, TaskAttachment, TaskTimeLog


@admin.register(TaskCategory)
class TaskCategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'color', 'department']


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ['title', 'assigned_to', 'assigned_by', 'status', 'priority', 'due_date', 'is_overdue']
    list_filter = ['status', 'priority', 'category']
    search_fields = ['title', 'description']
    autocomplete_fields = ['assigned_to', 'assigned_by']
    date_hierarchy = 'created_at'


@admin.register(TaskComment)
class TaskCommentAdmin(admin.ModelAdmin):
    list_display = ['task', 'author', 'created_at']


@admin.register(TaskTimeLog)
class TaskTimeLogAdmin(admin.ModelAdmin):
    list_display = ['task', 'user', 'hours', 'date']
