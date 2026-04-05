from django.contrib import admin
from apps.organization.models import Department, Unit, Position, OfficeLocation


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'head', 'is_active']
    search_fields = ['name', 'code']


@admin.register(Unit)
class UnitAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'department', 'head_of_unit', 'member_count', 'is_active']
    list_filter = ['department', 'is_active']
    search_fields = ['name', 'code']


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ['title', 'code', 'grade', 'department', 'is_supervisor', 'is_active']
    list_filter = ['grade', 'is_supervisor']


@admin.register(OfficeLocation)
class OfficeLocationAdmin(admin.ModelAdmin):
    list_display = ['name', 'city', 'country', 'is_headquarters', 'is_virtual']
