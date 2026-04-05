from django.contrib import admin
from apps.files.models import SharedFile, FileFolder


@admin.register(FileFolder)
class FileFolderAdmin(admin.ModelAdmin):
    list_display = ['name', 'owner', 'parent', 'visibility', 'created_at']
    list_filter = ['visibility']
    search_fields = ['name', 'owner__first_name', 'owner__last_name']
    autocomplete_fields = ['owner']
    readonly_fields = ['created_at']


@admin.register(SharedFile)
class SharedFileAdmin(admin.ModelAdmin):
    list_display = ['name', 'uploaded_by', 'folder', 'visibility', 'file_type', 'size_display', 'download_count', 'created_at']
    list_filter = ['visibility', 'file_type', 'is_latest']
    search_fields = ['name', 'description', 'tags', 'uploaded_by__first_name', 'uploaded_by__last_name']
    autocomplete_fields = ['uploaded_by']
    readonly_fields = ['created_at', 'updated_at', 'download_count', 'file_size', 'file_type']
    filter_horizontal = ['shared_with']
    date_hierarchy = 'created_at'

    fieldsets = (
        (None, {'fields': ('name', 'file', 'folder', 'uploaded_by')}),
        ('Sharing', {'fields': ('visibility', 'shared_with', 'unit', 'department')}),
        ('Metadata', {'fields': ('description', 'tags', 'version', 'is_latest', 'previous_version')}),
        ('Stats', {'fields': ('file_size', 'file_type', 'download_count', 'created_at', 'updated_at'), 'classes': ('collapse',)}),
    )

    def size_display(self, obj):
        return obj.size_display
    size_display.short_description = 'Size'