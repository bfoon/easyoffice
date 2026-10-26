"""
apps/features/views.py — superuser-only control panel.
Class-based views with manual POST handling (EasyOffice convention).
"""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views import View

from apps.features.registry import registry
from apps.features import services as fs


class SuperuserRequiredMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not request.user.is_superuser:
            raise PermissionDenied('Only superusers can manage features.')
        return super().dispatch(request, *args, **kwargs)


def _wants_json(request):
    return (request.headers.get('x-requested-with') == 'XMLHttpRequest'
            or 'application/json' in request.META.get('HTTP_ACCEPT', ''))


class FeatureControlPanelView(SuperuserRequiredMixin, View):
    template_name = 'features/control_panel.html'

    def get(self, request):
        from apps.features.models import FeatureAuditLog, FeatureFlag

        flags = {f.key: f for f in FeatureFlag.objects.prefetch_related('pilot_users', 'pilot_groups')}
        groups = []
        totals = {'on': 0, 'pilot': 0, 'off': 0, 'locked': 0}
        for category, specs in registry.by_category():
            rows = []
            for spec in specs:
                mode = fs.mode_for(spec.key)
                flag = flags.get(spec.key)
                blocked_by = [r for r in registry.all_requirements_of(spec.key)
                              if fs.mode_for(r.key) == fs.OFF]
                rows.append({
                    'spec': spec,
                    'mode': mode,
                    'flag': flag,
                    'requires': [registry.get(k) for k in spec.requires if registry.get(k)],
                    'dependents': registry.dependents_of(spec.key),
                    'blocked_by': blocked_by,
                    'pilot_count': (flag.pilot_users.count() + flag.pilot_groups.count()) if flag else 0,
                })
                totals['locked' if spec.locked else mode] += 1
            groups.append((category, rows))

        unknown = [f for k, f in flags.items() if registry.get(k) is None]
        return render(request, self.template_name, {
            'groups': groups,
            'totals': totals,
            'unknown_flags': unknown,
            'recent_log': FeatureAuditLog.objects.select_related('actor')[:12],
            'all_groups': Group.objects.order_by('name'),
        })


class FeatureSetModeView(SuperuserRequiredMixin, View):
    def post(self, request, key):
        mode = (request.POST.get('mode') or '').strip()
        cascade = request.POST.get('cascade') == '1'
        note = (request.POST.get('note') or '').strip()
        try:
            result = fs.set_mode(key, mode, actor=request.user, cascade=cascade,
                                 ip=fs.client_ip(request), note=note)
        except fs.FeatureDependencyError as exc:
            payload = {'ok': False, 'error': str(exc), 'needs_cascade': True,
                       'blocking': [{'key': s.key, 'name': s.name} for s in exc.blocking]}
            if _wants_json(request):
                return JsonResponse(payload, status=409)
            messages.warning(request, str(exc))
            return redirect('features:panel')
        except ValueError as exc:
            if _wants_json(request):
                return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
            messages.error(request, str(exc))
            return redirect('features:panel')

        spec = registry.get(key)
        parts = [f'{spec.name} is now {dict(on="on", pilot="in pilot", off="off")[mode]}.']
        if result['auto_enabled']:
            parts.append('Also switched on: ' + ', '.join(s.name for s in result['auto_enabled']) + '.')
        if result['cascaded']:
            parts.append('Also switched off: ' + ', '.join(s.name for s in result['cascaded']) + '.')
        msg = ' '.join(parts)

        if _wants_json(request):
            return JsonResponse({
                'ok': True, 'message': msg,
                'modes': {s.key: fs.mode_for(s.key) for s in registry.all()},
            })
        messages.success(request, msg)
        return redirect('features:panel')


class FeatureSettingsView(SuperuserRequiredMixin, View):
    """Pilot audience, superuser preview and the 'off' message."""

    def post(self, request, key):
        from apps.features.models import FeatureAuditLog, FeatureFlag

        spec = registry.get(key)
        if spec is None or spec.locked:
            messages.error(request, 'That feature cannot be configured.')
            return redirect('features:panel')

        flag, created = FeatureFlag.objects.get_or_create(
            key=key, defaults={'mode': fs.mode_for(key)})
        flag.superuser_preview = request.POST.get('superuser_preview') == '1'
        flag.off_message = (request.POST.get('off_message') or '').strip()[:300]
        flag.updated_by = request.user
        flag.save()

        User = get_user_model()
        emails = [e.strip().lower() for e in
                  (request.POST.get('pilot_emails') or '').replace(';', ',').replace('\n', ',').split(',')
                  if e.strip()]
        users = list(User.objects.filter(email__in=emails))
        missing = sorted(set(emails) - {u.email.lower() for u in users})
        flag.pilot_users.set(users)

        group_ids = [g for g in request.POST.getlist('pilot_groups') if g.isdigit()]
        flag.pilot_groups.set(Group.objects.filter(pk__in=group_ids))

        FeatureAuditLog.objects.create(
            key=key, old_mode=flag.mode, new_mode=flag.mode, actor=request.user,
            ip_address=fs.client_ip(request),
            note=f'Settings updated: {len(users)} pilot user(s), {len(group_ids)} group(s), '
                 f'preview {"on" if flag.superuser_preview else "off"}',
        )
        fs.invalidate_cache()

        if missing:
            messages.warning(request, 'No account found for: ' + ', '.join(missing))
        messages.success(request, f'{spec.name} settings saved.')
        return redirect('features:panel')


class FeatureAuditLogView(SuperuserRequiredMixin, View):
    def get(self, request):
        from apps.features.models import FeatureAuditLog
        qs = FeatureAuditLog.objects.select_related('actor')
        key = request.GET.get('key', '').strip()
        if key:
            qs = qs.filter(key=key)
        names = {s.key: s.name for s in registry.all()}
        entries = [{'e': e, 'name': names.get(e.key, e.key)} for e in qs[:300]]
        return render(request, 'features/audit.html', {
            'entries': entries, 'key': key, 'specs': registry.all(),
        })
