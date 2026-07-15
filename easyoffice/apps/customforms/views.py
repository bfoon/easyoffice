"""
apps/customforms/views.py

View map
────────
  FormsHomeView              GET  /forms/                      dashboard: fill / manage / inbox
  FormBuilderView            GET  /forms/builder/[?id=<uuid>]  drag-and-drop builder SPA

  FormTemplateListView       GET/POST /forms/api/templates/
  FormTemplateDetailView     GET  /forms/api/templates/<uuid>/
  FormTemplateUpdateView     POST /forms/api/templates/<uuid>/update/
  FormTemplateDeleteView     POST /forms/api/templates/<uuid>/delete/
  FormTemplatePublishView    POST /forms/api/templates/<uuid>/publish/   body {status}
  FormTemplateDuplicateView  POST /forms/api/templates/<uuid>/duplicate/
  DirectoryView              GET  /forms/api/directory/?q=      users + roles + units + positions

  FormFillView               GET  /forms/<uuid>/fill/
  FormSubmitView             POST /forms/<uuid>/submit/
  SubmissionDetailView       GET  /forms/submissions/<uuid>/
  SubmissionActionView       POST /forms/submissions/<uuid>/steps/<uuid>/act/
                                  body {decision: approve|reject, comment, signature_data}

  FormTemplateExportView     GET  /forms/api/templates/<uuid>/export/<fmt>/   empty form
  SubmissionExportView       GET  /forms/submissions/<uuid>/export/<fmt>/     filled form
                                  fmt: pdf | docx

Permissions
───────────
  Managing (create/edit/delete/publish) templates: superuser, or staff
  profile role 'ceo' / 'office_admin' — same rule as letterheads.
  Filling published forms: any logged-in user.
  Acting on a step: the resolved assignee (user / matching role/unit/position).
"""
import html
import json
import logging
import os
import subprocess
import tempfile

from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from .models import (
    FormTemplate, FormSubmission, SubmissionStep,
    profile_matches, distinct_profile_values,
)

logger = logging.getLogger(__name__)
User = get_user_model()

FIELD_TYPES_WITH_DATA = {
    'text', 'textarea', 'number', 'date', 'time', 'email', 'phone',
    'select', 'radio', 'checkboxes', 'yesno', 'table', 'currency',
}
LAYOUT_TYPES = {'heading', 'paragraph', 'divider'}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _require_forms_permission(user):
    """Same rule as letterheads: superuser or role ceo / office_admin."""
    if user.is_superuser:
        return None
    profile = getattr(user, 'staffprofile', None)
    if profile and getattr(profile, 'role', None) in ('ceo', 'office_admin'):
        return None
    return JsonResponse({'error': 'You do not have permission to manage forms.'}, status=403)


def _can_manage(user) -> bool:
    return _require_forms_permission(user) is None


def _parse_body(request) -> dict:
    try:
        return json.loads(request.body or '{}')
    except (ValueError, TypeError):
        return {}


def _validate_schema(schema):
    if not isinstance(schema, list):
        return 'schema must be a list.'
    seen = set()
    for f in schema:
        if not isinstance(f, dict) or not f.get('id') or not f.get('type'):
            return 'Every field needs an id and a type.'
        if f['id'] in seen:
            return f"Duplicate field id: {f['id']}"
        seen.add(f['id'])
    return None


def _validate_flow(flow):
    if not isinstance(flow, list):
        return 'approval_flow must be a list.'
    for s in flow:
        if not isinstance(s, dict) or not s.get('id'):
            return 'Every step needs an id.'
        if s.get('assignee_type') not in (
                'user', 'role', 'position', 'unit', 'submitter_choice'):
            return f"Step '{s.get('label', '?')}' has an invalid assignee type."
        if s.get('assignee_type') != 'submitter_choice' and not s.get('assignee_value'):
            return f"Step '{s.get('label', '?')}' needs an assignee."
        if s.get('action') not in ('approve', 'sign'):
            return f"Step '{s.get('label', '?')}' has an invalid action."
    return None


def _user_brief(u):
    profile = getattr(u, 'staffprofile', None)
    return {
        'id':   u.pk,
        'name': u.get_full_name() or u.get_username(),
        'username': u.get_username(),
        'role': getattr(profile, 'role', '') if profile else '',
    }


# ── Pages ────────────────────────────────────────────────────────────────────

class FormsHomeView(LoginRequiredMixin, TemplateView):
    """GET /forms/ — dashboard: fillable forms, my submissions, approvals inbox."""
    template_name = 'customforms/forms_home.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user

        ctx['can_manage'] = _can_manage(user)
        ctx['published']  = FormTemplate.objects.filter(status='published')
        if ctx['can_manage']:
            ctx['drafts'] = FormTemplate.objects.exclude(status='published')

        ctx['my_submissions'] = (
            FormSubmission.objects.filter(submitted_by=user)
            .select_related('template')[:50]
        )

        # Approvals inbox: pending steps this user can act on.
        pending = (
            SubmissionStep.objects.filter(status='pending')
            .select_related('submission', 'submission__template',
                            'submission__submitted_by', 'assigned_user')
        )
        ctx['inbox'] = [s for s in pending if s.user_can_act(user)]
        return ctx


class FormBuilderView(LoginRequiredMixin, TemplateView):
    """GET /forms/builder/[?id=<uuid>] — the drag-and-drop builder SPA."""
    template_name = 'customforms/form_builder.html'

    def get(self, request, *args, **kwargs):
        denied = _require_forms_permission(request.user)
        if denied:
            return render(request, 'customforms/forms_home.html',
                          {'permission_denied': True}, status=403)
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        template_json = 'null'
        pk = self.request.GET.get('id')
        if pk:
            try:
                t = FormTemplate.objects.get(pk=pk)
                template_json = json.dumps(t.to_builder_dict())
            except (FormTemplate.DoesNotExist, ValueError):
                pass
        ctx['template_json'] = template_json

        # Letterhead choices for the settings tab.
        letterheads = []
        try:
            from apps.letterhead.models import LetterheadTemplate
            letterheads = [
                {'id': str(l.pk), 'name': l.name, 'is_active': l.is_active}
                for l in LetterheadTemplate.objects.all()
            ]
        except Exception:
            pass
        ctx['letterheads_json'] = json.dumps(letterheads)

        ctx['directory_json'] = json.dumps({
            'roles':     distinct_profile_values('role'),
            'positions': distinct_profile_values('position'),
            'units':     distinct_profile_values('unit'),
        })
        return ctx


# ── Template APIs ────────────────────────────────────────────────────────────

class FormTemplateListView(LoginRequiredMixin, View):
    """GET list / POST create."""

    def get(self, request):
        qs = FormTemplate.objects.all()
        if not _can_manage(request.user):
            qs = qs.filter(status='published')
        return JsonResponse({'templates': [t.to_builder_dict() for t in qs]})

    def post(self, request):
        denied = _require_forms_permission(request.user)
        if denied:
            return denied
        data = _parse_body(request)
        title = (data.get('title') or '').strip()
        if not title:
            return JsonResponse({'errors': {'title': 'Required.'}}, status=400)

        t = FormTemplate.objects.create(
            title=title,
            description=data.get('description', ''),
            schema=data.get('schema') or [],
            approval_flow=data.get('approval_flow') or [],
            letterhead_mode=data.get('letterhead_mode', 'none'),
            created_by=request.user,
            last_edited_by=request.user,
        )
        return JsonResponse({'ok': True, 'template': t.to_builder_dict()}, status=201)


class FormTemplateDetailView(LoginRequiredMixin, View):
    def get(self, request, pk):
        t = get_object_or_404(FormTemplate, pk=pk)
        return JsonResponse({'template': t.to_builder_dict()})


class FormTemplateUpdateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        denied = _require_forms_permission(request.user)
        if denied:
            return denied
        t = get_object_or_404(FormTemplate, pk=pk)
        data = _parse_body(request)

        if 'schema' in data:
            err = _validate_schema(data['schema'])
            if err:
                return JsonResponse({'error': err}, status=400)
            t.schema = data['schema']
        if 'approval_flow' in data:
            err = _validate_flow(data['approval_flow'])
            if err:
                return JsonResponse({'error': err}, status=400)
            t.approval_flow = data['approval_flow']
        for field in ('title', 'description', 'letterhead_mode'):
            if field in data:
                setattr(t, field, data[field])
        if 'letterhead_id' in data:
            t.letterhead_id = data['letterhead_id'] or None

        t.last_edited_by = request.user
        t.save()
        return JsonResponse({'ok': True, 'template': t.to_builder_dict()})


class FormTemplateDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        denied = _require_forms_permission(request.user)
        if denied:
            return denied
        t = get_object_or_404(FormTemplate, pk=pk)
        if t.submissions.exists():
            t.status = 'archived'
            t.save(update_fields=['status', 'updated_at'])
            return JsonResponse({'ok': True, 'archived': True,
                                 'detail': 'Template has submissions, so it was archived instead of deleted.'})
        title = t.title
        t.delete()
        return JsonResponse({'ok': True, 'deleted': title})


class FormTemplatePublishView(LoginRequiredMixin, View):
    """POST body: {status: 'published'|'draft'|'archived'}"""

    def post(self, request, pk):
        denied = _require_forms_permission(request.user)
        if denied:
            return denied
        t = get_object_or_404(FormTemplate, pk=pk)
        status = _parse_body(request).get('status', 'published')
        if status not in ('draft', 'published', 'archived'):
            return JsonResponse({'error': 'Invalid status.'}, status=400)
        if status == 'published':
            err = _validate_flow(t.approval_flow or [])
            if err:
                return JsonResponse({'error': f'Fix approval flow first: {err}'}, status=400)
            if not any(f.get('type') in FIELD_TYPES_WITH_DATA for f in (t.schema or [])):
                return JsonResponse({'error': 'Add at least one input field before publishing.'}, status=400)
        t.status = status
        t.last_edited_by = request.user
        t.save(update_fields=['status', 'last_edited_by', 'updated_at'])
        return JsonResponse({'ok': True, 'template': t.to_builder_dict()})


class FormTemplateDuplicateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        denied = _require_forms_permission(request.user)
        if denied:
            return denied
        src = get_object_or_404(FormTemplate, pk=pk)
        copy = FormTemplate.objects.create(
            title=f'{src.title} (copy)',
            description=src.description,
            schema=src.schema,
            approval_flow=src.approval_flow,
            letterhead_mode=src.letterhead_mode,
            letterhead=src.letterhead,
            status='draft',
            created_by=request.user,
            last_edited_by=request.user,
        )
        return JsonResponse({'ok': True, 'template': copy.to_builder_dict()}, status=201)


class DirectoryView(LoginRequiredMixin, View):
    """
    GET /forms/api/directory/?q=jo
    Users matching q (max 15) plus distinct roles / positions / units.
    Powers the assignee pickers in the builder and the fill page.
    """

    def get(self, request):
        q = (request.GET.get('q') or '').strip()
        users_qs = User.objects.filter(is_active=True)
        if q:
            users_qs = users_qs.filter(
                Q(username__icontains=q) |
                Q(first_name__icontains=q) |
                Q(last_name__icontains=q) |
                Q(email__icontains=q)
            )
        return JsonResponse({
            'users':     [_user_brief(u) for u in users_qs[:15]],
            'roles':     distinct_profile_values('role'),
            'positions': distinct_profile_values('position'),
            'units':     distinct_profile_values('unit'),
        })


# ── Fill & submit ────────────────────────────────────────────────────────────

class FormFillView(LoginRequiredMixin, TemplateView):
    template_name = 'customforms/form_fill.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        t = get_object_or_404(FormTemplate, pk=kwargs['pk'], status='published')
        ctx['template'] = t
        ctx['template_json'] = json.dumps(t.to_builder_dict())
        ctx['choice_steps'] = [
            s for s in (t.approval_flow or [])
            if s.get('assignee_type') == 'submitter_choice'
        ]
        return ctx


class FormSubmitView(LoginRequiredMixin, View):
    """
    POST /forms/<uuid>/submit/
    Body: { data: {field_id: value}, chosen_approvers: {step_id: user_id} }
    """

    @transaction.atomic
    def post(self, request, pk):
        t = get_object_or_404(FormTemplate, pk=pk, status='published')
        body = _parse_body(request)
        data = body.get('data') or {}
        chosen = body.get('chosen_approvers') or {}

        # Required-field validation against the live schema.
        missing = []
        for f in (t.schema or []):
            if f.get('type') in LAYOUT_TYPES or not f.get('required'):
                continue
            v = data.get(f['id'])
            if v in (None, '', []) or (f.get('type') == 'table' and not any(
                    any(str(c).strip() for c in row) for row in (v or []))):
                missing.append(f.get('label') or f['id'])
        if missing:
            return JsonResponse(
                {'error': 'Please fill the required fields.', 'missing': missing},
                status=400)

        # Resolve submitter_choice steps up front.
        resolved_choice = {}
        for s in (t.approval_flow or []):
            if s.get('assignee_type') != 'submitter_choice':
                continue
            uid = chosen.get(s['id'])
            if not uid:
                return JsonResponse(
                    {'error': f"Please choose an approver for step “{s.get('label', 'Approval')}”."},
                    status=400)
            try:
                resolved_choice[s['id']] = User.objects.get(pk=uid, is_active=True)
            except Exception:
                return JsonResponse({'error': 'Chosen approver not found.'}, status=400)

        sub = FormSubmission.objects.create(
            template=t,
            schema_snapshot=t.schema or [],
            data=data,
            submitted_by=request.user,
        )

        flow = t.approval_flow or []
        for i, s in enumerate(flow):
            assigned = None
            if s.get('assignee_type') == 'user':
                try:
                    assigned = User.objects.filter(pk=s.get('assignee_value')).first()
                except Exception:
                    assigned = None
            elif s.get('assignee_type') == 'submitter_choice':
                assigned = resolved_choice[s['id']]
            SubmissionStep.objects.create(
                submission=sub,
                order=i,
                label=s.get('label') or f'Step {i + 1}',
                action=s.get('action', 'approve'),
                assignee_type=s.get('assignee_type'),
                assignee_value=str(s.get('assignee_value') or ''),
                assigned_user=assigned,
                status='pending' if i == 0 else 'waiting',
            )

        if not flow:
            sub.status = 'approved'
            sub.completed_at = timezone.now()
            sub.save(update_fields=['status', 'completed_at'])
        else:
            self._notify_step(sub.current_step)

        return JsonResponse({'ok': True, 'submission_id': str(sub.pk),
                             'ref_no': sub.ref_no}, status=201)

    def _notify_step(self, step):
        """Hook: plug in your email/notification system here."""
        if step:
            logger.info('Form %s: step "%s" now pending (%s)',
                        step.submission.ref_no, step.label, step.assignee_display())


# ── Submission detail & approval actions ────────────────────────────────────

class SubmissionDetailView(LoginRequiredMixin, TemplateView):
    template_name = 'customforms/submission_detail.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        sub = get_object_or_404(
            FormSubmission.objects.select_related('template', 'submitted_by'),
            pk=kwargs['pk'])
        user = self.request.user

        steps = list(sub.steps.select_related('assigned_user', 'acted_by'))
        is_party = (
            sub.submitted_by_id == user.pk or _can_manage(user) or
            any(st.acted_by_id == user.pk or st.user_can_act(user) or
                st.assigned_user_id == user.pk for st in steps)
        )
        if not is_party:
            ctx['permission_denied'] = True
            return ctx

        ctx['sub'] = sub
        ctx['steps'] = steps
        ctx['actionable_step'] = next(
            (st for st in steps if st.user_can_act(user)), None)
        def display(f, v):
            """Normalise for the template: keep table lists, join checkbox lists."""
            if f.get('type') != 'table' and isinstance(v, list):
                return ', '.join(str(x) for x in v)
            return v

        ctx['fields'] = [
            {'f': f, 'value': display(f, sub.data.get(f.get('id')))}
            for f in sub.schema_snapshot
        ]
        return ctx


class SubmissionActionView(LoginRequiredMixin, View):
    """
    POST /forms/submissions/<uuid>/steps/<uuid>/act/
    Body: { decision: 'approve'|'reject', comment: '', signature_data: 'data:image/png;base64,...' }
    """

    @transaction.atomic
    def post(self, request, pk, step_id):
        sub = get_object_or_404(FormSubmission, pk=pk)
        step = get_object_or_404(SubmissionStep, pk=step_id, submission=sub)

        if not step.user_can_act(request.user):
            return JsonResponse({'error': 'This step is not assigned to you (or has already been handled).'},
                                status=403)

        body = _parse_body(request)
        decision = body.get('decision')
        comment = (body.get('comment') or '').strip()
        signature = body.get('signature_data') or ''

        if decision not in ('approve', 'reject'):
            return JsonResponse({'error': 'decision must be approve or reject.'}, status=400)
        if decision == 'reject' and not comment:
            return JsonResponse({'error': 'A comment is required when rejecting.'}, status=400)
        if decision == 'approve' and step.action == 'sign' and not signature.startswith('data:image/'):
            return JsonResponse({'error': 'This step requires a drawn signature.'}, status=400)

        step.status = 'approved' if decision == 'approve' else 'rejected'
        step.acted_by = request.user
        step.acted_at = timezone.now()
        step.comment = comment
        if not step.assigned_user_id:
            step.assigned_user = request.user
        if decision == 'approve' and step.action == 'sign':
            step.signature_data = signature
        step.save()

        if decision == 'approve':
            sub.advance()
        else:
            sub.reject()

        return JsonResponse({'ok': True, 'submission_status': sub.status})


# ── Export (empty template / filled submission → PDF or Word) ────────────────

class _ExportBase(LoginRequiredMixin, View):
    """Shared HTML-building + LibreOffice conversion (same infra as letterheads)."""

    # -- letterhead header -------------------------------------------------
    #
    # LibreOffice's HTML import does NOT understand flexbox, grid, or
    # border-radius — so every layout below is built from plain <table>s
    # and simple borders.  The logo is embedded as a base64 data URI so it
    # renders regardless of media paths.

    def _logo_data_uri(self, lh):
        if not lh or not lh.logo:
            return ''
        try:
            import base64
            import mimetypes
            path = lh.logo.path
            mime = mimetypes.guess_type(path)[0] or 'image/png'
            with open(path, 'rb') as fh:
                return f'data:{mime};base64,{base64.b64encode(fh.read()).decode()}'
        except Exception:
            return ''

    def _letterhead_html(self, lh) -> str:
        if not lh:
            return ''
        esc = html.escape
        p, s = lh.color_primary, lh.color_secondary
        hf = f"{lh.font_header}, Georgia, serif"
        name = esc(lh.company_name)
        tagline = esc(lh.tagline or '')
        address = esc(lh.address or '').replace('\n', '<br>')
        contact = esc(lh.contact_info or '')
        logo_uri = self._logo_data_uri(lh)
        logo_cell = (f'<td style="width:60px;vertical-align:middle;padding-right:12px;">'
                     f'<img src="{logo_uri}" height="48" alt=""></td>') if logo_uri else ''
        key = getattr(lh, 'template_key', 'classic') or 'classic'

        tagline_div = (f'<div style="font-size:12px;font-style:italic;color:{p};margin-top:2px;">'
                       f'{tagline}</div>') if tagline else ''
        contact_div = f'<div style="font-size:10.5px;color:#777;">{address}</div>' \
                      f'<div style="font-size:10.5px;color:#777;">{contact}</div>'

        if key == 'bold':
            logo_band = (f'<td style="width:60px;padding:10px 12px 10px 0;vertical-align:middle;">'
                         f'<img src="{logo_uri}" height="44" alt=""></td>') if logo_uri else ''
            return f"""
  <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin-bottom:14px;">
    <tr><td style="background:{p};padding:12px 16px;">
      <table cellspacing="0" cellpadding="0"><tr>{logo_band}
        <td style="vertical-align:middle;">
          <span style="font-family:{hf};font-size:22px;font-weight:bold;color:#ffffff;">{name}</span>
          {f'<div style="font-size:11.5px;color:#ffffff;font-style:italic;">{tagline}</div>' if tagline else ''}
        </td></tr></table>
    </td></tr>
    <tr><td style="padding:6px 2px 0;">{contact_div}</td></tr>
  </table>"""

        if key == 'minimal':
            return f"""
  <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin-bottom:14px;">
    <tr>{logo_cell}<td style="vertical-align:middle;">
      <span style="font-family:{hf};font-size:17px;font-weight:bold;color:{s};">{name}</span>
      {tagline_div}
    </td></tr>
    <tr><td colspan="2" style="padding-top:3px;">{contact_div}</td></tr>
  </table>"""

        if key == 'split':
            return f"""
  <table width="100%" cellspacing="0" cellpadding="0"
         style="border-collapse:collapse;border-bottom:2px solid {p};margin-bottom:14px;">
    <tr>
      <td style="vertical-align:middle;padding-bottom:8px;">
        <table cellspacing="0" cellpadding="0"><tr>{logo_cell}<td style="vertical-align:middle;">
          <span style="font-family:{hf};font-size:20px;font-weight:bold;color:{s};">{name}</span>
          {tagline_div}
        </td></tr></table>
      </td>
      <td style="vertical-align:middle;text-align:right;padding-bottom:8px;">{contact_div}</td>
    </tr>
  </table>"""

        if key == 'elegant':
            logo_row = (f'<tr><td align="center" style="padding-bottom:6px;">'
                        f'<img src="{logo_uri}" height="46" alt=""></td></tr>') if logo_uri else ''
            return f"""
  <table width="100%" cellspacing="0" cellpadding="0" style="border-collapse:collapse;margin-bottom:14px;">
    {logo_row}
    <tr><td align="center">
      <span style="font-family:{hf};font-size:23px;font-weight:bold;color:{s};letter-spacing:1px;">{name}</span>
      {tagline_div}
      <div style="padding-top:4px;">{contact_div}</div>
    </td></tr>
    <tr><td style="border-bottom:1px solid {p};padding-top:8px;font-size:1px;">&nbsp;</td></tr>
    <tr><td style="border-bottom:1px solid {p};padding-top:2px;font-size:1px;">&nbsp;</td></tr>
  </table>"""

        if key == 'modern':
            return f"""
  <table width="100%" cellspacing="0" cellpadding="0"
         style="border-collapse:collapse;border-bottom:3px solid {p};margin-bottom:14px;">
    <tr>{logo_cell}
      <td style="vertical-align:middle;padding-bottom:8px;">
        <span style="font-family:{hf};font-size:21px;font-weight:bold;color:{p};">{name}</span>
        {tagline_div}
      </td>
      <td style="vertical-align:bottom;text-align:right;padding-bottom:8px;">{contact_div}</td>
    </tr>
  </table>"""

        # 'classic' (default)
        return f"""
  <table width="100%" cellspacing="0" cellpadding="0"
         style="border-collapse:collapse;border-top:4px solid {p};margin-bottom:14px;">
    <tr>{logo_cell}<td style="vertical-align:middle;padding-top:10px;">
      <span style="font-family:{hf};font-size:21px;font-weight:bold;color:{s};">{name}</span>
      {tagline_div}
    </td></tr>
    <tr><td colspan="2" style="padding-top:4px;">{contact_div}</td></tr>
    <tr><td colspan="2" style="border-bottom:2px solid {p};padding-top:8px;font-size:1px;">&nbsp;</td></tr>
  </table>"""

    # -- one field, empty or filled ----------------------------------------

    def _field_html(self, f: dict, value=None, filled=False) -> str:
        esc = html.escape
        ftype = f.get('type')
        label = esc(f.get('label') or '')
        req = ' <span style="color:#c0392b;">*</span>' if f.get('required') else ''
        helptext = (f'<div style="font-size:10px;color:#999;">{esc(f.get("help"))}</div>'
                    if f.get('help') else '')

        if ftype == 'heading':
            return f'<h3 style="margin:18px 0 6px;font-size:15px;border-bottom:1px solid #ddd;padding-bottom:4px;">{label}</h3>'
        if ftype == 'paragraph':
            return f'<p style="font-size:11.5px;color:#555;margin:6px 0;">{esc(f.get("label") or "")}</p>'
        if ftype == 'divider':
            return '<hr style="border:none;border-top:1px solid #ddd;margin:14px 0;">'

        if ftype == 'table':
            cols = f.get('columns') or ['Column 1']
            head = ''.join(
                f'<th style="border:1px solid #bbb;background:#f2f2f2;padding:5px 7px;'
                f'font-size:10.5px;text-align:left;">{esc(str(c))}</th>' for c in cols)
            if filled and isinstance(value, list) and value:
                rows_html = ''
                for row in value:
                    cells = ''.join(
                        f'<td style="border:1px solid #ccc;padding:5px 7px;font-size:11px;">'
                        f'{esc(str(c))}</td>'
                        for c in (list(row) + [''] * len(cols))[:len(cols)])
                    rows_html += f'<tr>{cells}</tr>'
            else:
                empty_cells = ''.join(
                    '<td style="border:1px solid #ccc;padding:12px 7px;">&nbsp;</td>'
                    for _ in cols)
                rows_html = ''.join(f'<tr>{empty_cells}</tr>'
                                    for _ in range(int(f.get('rows') or 3)))
            return (f'<div style="margin:10px 0;"><div style="font-size:11.5px;font-weight:700;'
                    f'margin-bottom:4px;">{label}{req}</div>{helptext}'
                    f'<table style="border-collapse:collapse;width:100%;">'
                    f'<tr>{head}</tr>{rows_html}</table></div>')

        if ftype in ('radio', 'checkboxes', 'select', 'yesno'):
            options = ['Yes', 'No'] if ftype == 'yesno' else (f.get('options') or [])
            if filled:
                chosen = value if isinstance(value, list) else [value]
                chosen = [str(c) for c in chosen if c not in (None, '')]
                marks = ''.join(
                    f'<span style="margin-right:16px;font-size:11.5px;">'
                    f'{"☑" if str(o) in chosen else "☐"} {esc(str(o))}</span>'
                    for o in options)
            else:
                box = '☐'
                marks = ''.join(
                    f'<span style="margin-right:16px;font-size:11.5px;">{box} {esc(str(o))}</span>'
                    for o in options)
            return (f'<div style="margin:9px 0;"><span style="font-size:11.5px;font-weight:700;">'
                    f'{label}{req}</span>{helptext}<div style="margin-top:4px;">{marks}</div></div>')

        # Text-like fields (text, textarea, number, date, time, email, phone, currency)
        lines = 3 if ftype == 'textarea' else 1
        if filled:
            v = esc('' if value is None else str(value)).replace('\n', '<br>')
            body = (f'<div style="border-bottom:1px solid #999;min-height:{16 * lines}px;'
                    f'padding:2px 4px;font-size:11.5px;">{v or "&nbsp;"}</div>')
        else:
            body = ''.join(
                '<div style="border-bottom:1px solid #999;height:20px;"></div>'
                for _ in range(lines))
        return (f'<div style="margin:9px 0;"><div style="font-size:11.5px;font-weight:700;'
                f'margin-bottom:3px;">{label}{req}</div>{helptext}{body}</div>')

    # -- approval block -----------------------------------------------------

    def _approval_block_empty(self, flow) -> str:
        if not flow:
            return ''
        rows = ''
        for i, s in enumerate(flow):
            sig = ('<div style="border-bottom:1px solid #999;height:34px;"></div>'
                   '<div style="font-size:9.5px;color:#888;">Signature</div>'
                   if s.get('action') == 'sign' else
                   '<div style="border-bottom:1px solid #999;height:20px;margin-top:14px;"></div>'
                   '<div style="font-size:9.5px;color:#888;">Approved (name / initials)</div>')
            rows += f"""
      <td style="border:1px solid #ccc;padding:8px;vertical-align:top;width:{100 // max(len(flow),1)}%;">
        <div style="font-size:10px;color:#666;">Step {i + 1}</div>
        <div style="font-size:11px;font-weight:700;margin-bottom:8px;">{html.escape(s.get('label') or '')}</div>
        {sig}
        <div style="border-bottom:1px solid #999;height:18px;margin-top:12px;"></div>
        <div style="font-size:9.5px;color:#888;">Date</div>
      </td>"""
        return (f'<h3 style="margin:22px 0 6px;font-size:13px;">Approvals</h3>'
                f'<table style="border-collapse:collapse;width:100%;"><tr>{rows}</tr></table>')

    def _approval_block_filled(self, steps) -> str:
        if not steps:
            return ''
        rows = ''
        status_color = {'approved': '#1D9E75', 'rejected': '#c0392b',
                        'pending': '#b8860b', 'waiting': '#888', 'skipped': '#888'}
        for st in steps:
            who = html.escape(
                (st.acted_by.get_full_name() or st.acted_by.get_username())
                if st.acted_by else st.assignee_display())
            when = st.acted_at.strftime('%d %b %Y %H:%M') if st.acted_at else '—'
            sig = (f'<img src="{st.signature_data}" style="height:40px;">'
                   if st.signature_data else '')
            comment = (f'<div style="font-size:10px;color:#555;font-style:italic;">'
                       f'“{html.escape(st.comment)}”</div>' if st.comment else '')
            rows += f"""
      <td style="border:1px solid #ccc;padding:8px;vertical-align:top;width:{100 // max(len(steps),1)}%;">
        <div style="font-size:11px;font-weight:700;">{html.escape(st.label)}</div>
        <div style="font-size:10.5px;color:{status_color.get(st.status, '#333')};
                    font-weight:700;text-transform:uppercase;margin:2px 0 6px;">{st.get_status_display()}</div>
        {sig}
        <div style="font-size:10.5px;">{who}</div>
        <div style="font-size:10px;color:#777;">{when}</div>
        {comment}
      </td>"""
        return (f'<h3 style="margin:22px 0 6px;font-size:13px;">Approvals</h3>'
                f'<table style="border-collapse:collapse;width:100%;"><tr>{rows}</tr></table>')

    # -- document shell + conversion -----------------------------------------

    def _document(self, title, letterhead_html, meta_html, body_html, approvals_html,
                  body_font='Arial') -> str:
        return f"""<!DOCTYPE html><html><head><meta charset="UTF-8"><style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: {body_font}, Arial, sans-serif; padding:36px 44px; color:#222; }}
</style></head><body>
  {letterhead_html}
  <h2 style="font-size:16px;margin:14px 0 2px;">{html.escape(title)}</h2>
  {meta_html}
  {body_html}
  {approvals_html}
</body></html>"""

    def _convert(self, html_content: str, fmt: str, filename: str):
        """LibreOffice headless HTML → pdf/docx, matching the letterhead exporter."""
        if fmt not in ('pdf', 'docx'):
            return JsonResponse({'error': f'Unsupported format: {fmt}'}, status=400)
        tmp = tempfile.NamedTemporaryFile(suffix='.html', delete=False,
                                          mode='w', encoding='utf-8')
        tmp.write(html_content)
        tmp.close()
        out_path = tmp.name[:-5] + f'.{fmt}'
        convert_arg = 'pdf' if fmt == 'pdf' else 'docx:MS Word 2007 XML'
        content_type = ('application/pdf' if fmt == 'pdf' else
                        'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        try:
            result = subprocess.run(
                ['libreoffice', '--headless', '--convert-to', convert_arg,
                 '--outdir', os.path.dirname(out_path), tmp.name],
                capture_output=True, text=True, timeout=90)
            if result.returncode != 0 or not os.path.exists(out_path):
                return JsonResponse({'error': 'Export failed.',
                                     'detail': (result.stderr or '')[:500]}, status=500)
            with open(out_path, 'rb') as fh:
                data = fh.read()
            resp = HttpResponse(data, content_type=content_type)
            resp['Content-Disposition'] = f'attachment; filename="{filename}.{fmt}"'
            return resp
        except FileNotFoundError:
            return JsonResponse({'error': 'LibreOffice is not available on this server.'}, status=501)
        finally:
            for p in (tmp.name, out_path):
                try:
                    if os.path.exists(p):
                        os.unlink(p)
                except OSError:
                    pass


class FormTemplateExportView(_ExportBase):
    """GET /forms/api/templates/<uuid>/export/<fmt>/ — blank printable form."""

    def get(self, request, pk, fmt):
        t = get_object_or_404(FormTemplate, pk=pk)
        lh = t.resolve_letterhead()
        lh_html = self._letterhead_html(lh)
        meta = ('<div style="font-size:10.5px;color:#777;margin-bottom:10px;">'
                'Ref No: ______________ &nbsp;&nbsp; Date: ______________</div>')
        body = ''.join(self._field_html(f, filled=False) for f in (t.schema or []))
        approvals = self._approval_block_empty(t.approval_flow or [])
        doc = self._document(t.title, lh_html, meta, body, approvals,
                             body_font=getattr(lh, 'font_body', 'Arial') if lh else 'Arial')
        safe = t.title.replace(' ', '_')[:60] or 'form'
        return self._convert(doc, fmt, safe)


class SubmissionExportView(_ExportBase):
    """GET /forms/submissions/<uuid>/export/<fmt>/ — filled form."""

    def get(self, request, pk, fmt):
        sub = get_object_or_404(
            FormSubmission.objects.select_related('template', 'submitted_by'), pk=pk)
        steps = list(sub.steps.select_related('acted_by', 'assigned_user'))

        user = request.user
        is_party = (sub.submitted_by_id == user.pk or _can_manage(user) or
                    any(st.acted_by_id == user.pk or st.assigned_user_id == user.pk
                        or st.user_can_act(user) for st in steps))
        if not is_party:
            return JsonResponse({'error': 'Not allowed.'}, status=403)

        lh = sub.template.resolve_letterhead()
        lh_html = self._letterhead_html(lh)
        submitter = sub.submitted_by.get_full_name() or sub.submitted_by.get_username()
        meta = (f'<div style="font-size:10.5px;color:#777;margin-bottom:10px;">'
                f'Ref No: <b>{sub.ref_no}</b> &nbsp;·&nbsp; '
                f'Submitted by {html.escape(submitter)} on '
                f'{sub.submitted_at.strftime("%d %b %Y %H:%M")} &nbsp;·&nbsp; '
                f'Status: <b>{sub.get_status_display()}</b></div>')
        body = ''.join(
            self._field_html(f, value=sub.data.get(f.get('id')), filled=True)
            for f in (sub.schema_snapshot or []))
        approvals = self._approval_block_filled(steps)
        doc = self._document(sub.template.title, lh_html, meta, body, approvals,
                             body_font=getattr(lh, 'font_body', 'Arial') if lh else 'Arial')
        return self._convert(doc, fmt, sub.ref_no)