import json
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, View
from django.db import transaction, models
from django.http import JsonResponse, HttpResponseForbidden
from apps.organization.models import Department, Unit


class OrgChartView(LoginRequiredMixin, TemplateView):
    template_name = 'organization/org_chart.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        departments = Department.objects.filter(
            is_active=True
        ).select_related(
            'head', 'parent'
        ).prefetch_related(
            'units',
            'units__head_of_unit',
            'units__staffprofile_set__user'
        ).order_by(
            'management_level', 'row', 'col', 'sort_order', 'name'
        )

        ctx['level_1_departments'] = departments.filter(management_level=1)
        ctx['level_2_departments'] = departments.filter(management_level=2)
        ctx['departments'] = departments
        return ctx


class DepartmentListView(LoginRequiredMixin, TemplateView):
    template_name = 'organization/department_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['departments'] = Department.objects.filter(
            is_active=True
        ).select_related(
            'head', 'parent'
        ).prefetch_related(
            'units'
        ).order_by(
            'management_level', 'row', 'col', 'sort_order', 'name'
        )
        return ctx


class UnitListView(LoginRequiredMixin, TemplateView):
    template_name = 'organization/unit_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        units = Unit.objects.filter(
            is_active=True
        ).select_related(
            'department', 'head_of_unit'
        ).order_by(
            'department__management_level',
            'department__row',
            'department__col',
            'row',
            'col',
            'sort_order',
            'name'
        )

        q = self.request.GET.get('q', '').strip()
        dept = self.request.GET.get('dept', '').strip()

        if q:
            units = units.filter(
                models.Q(name__icontains=q) |
                models.Q(code__icontains=q)
            )

        if dept:
            units = units.filter(department_id=dept)

        ctx['units'] = units
        ctx['departments'] = Department.objects.filter(
            is_active=True
        ).order_by(
            'management_level', 'row', 'col', 'sort_order', 'name'
        )
        return ctx


class SaveOrgChartLayoutView(LoginRequiredMixin, View):
    def post(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            return HttpResponseForbidden('Only administrators can rearrange the chart.')

        try:
            payload = json.loads(request.body or '{}')
        except json.JSONDecodeError:
            return JsonResponse({'ok': False, 'error': 'Invalid JSON.'}, status=400)

        departments = payload.get('departments', [])
        units = payload.get('units', [])

        dept_map = {
            str(d.id): d for d in Department.objects.filter(
                id__in=[item['id'] for item in departments if item.get('id')]
            )
        }
        unit_map = {
            str(u.id): u for u in Unit.objects.filter(
                id__in=[item['id'] for item in units if item.get('id')]
            )
        }

        with transaction.atomic():
            for item in departments:
                dept = dept_map.get(str(item.get('id')))
                if not dept:
                    continue

                changed = []
                new_parent_id = item.get('parent_id')
                new_parent = dept_map.get(str(new_parent_id)) if new_parent_id else None

                if dept.parent_id != (new_parent.id if new_parent else None):
                    dept.parent = new_parent
                    changed.append('parent')

                new_level = item.get('management_level', dept.management_level)
                if dept.management_level != new_level:
                    dept.management_level = new_level
                    changed.append('management_level')

                new_row = item.get('row', dept.row)
                if dept.row != new_row:
                    dept.row = new_row
                    changed.append('row')

                new_col = item.get('col', dept.col)
                if dept.col != new_col:
                    dept.col = new_col
                    changed.append('col')

                new_sort_order = item.get('sort_order', dept.sort_order)
                if dept.sort_order != new_sort_order:
                    dept.sort_order = new_sort_order
                    changed.append('sort_order')

                if changed:
                    dept.save(update_fields=changed)

            for item in units:
                unit = unit_map.get(str(item.get('id')))
                if not unit:
                    continue

                changed = []
                new_dept = dept_map.get(str(item.get('department_id')))

                if new_dept and unit.department_id != new_dept.id:
                    unit.department = new_dept
                    changed.append('department')

                new_row = item.get('row', unit.row)
                if unit.row != new_row:
                    unit.row = new_row
                    changed.append('row')

                new_col = item.get('col', unit.col)
                if unit.col != new_col:
                    unit.col = new_col
                    changed.append('col')

                new_sort_order = item.get('sort_order', unit.sort_order)
                if unit.sort_order != new_sort_order:
                    unit.sort_order = new_sort_order
                    changed.append('sort_order')

                if changed:
                    unit.save(update_fields=changed)

        return JsonResponse({'ok': True})