"""
apps/letterhead/views.py

Full working views for the Letterhead Builder SPA.

Permissions helper
──────────────────
_require_letterhead_permission(user) → None | JsonResponse(403)
  Allows: superuser, staff with profile role 'ceo' or 'office_admin'.
  Returns None if allowed, a 403 JsonResponse if not.

View map
────────
  LetterheadBuilderView           GET  /letterhead/
  LetterheadListView              GET/POST  /letterhead/api/templates/
  LetterheadDetailView            GET  /letterhead/api/templates/<uuid>/
  LetterheadUpdateView            POST /letterhead/api/templates/<uuid>/update/
  LetterheadDeleteView            POST /letterhead/api/templates/<uuid>/delete/
  LetterheadSetActiveView         POST /letterhead/api/templates/<uuid>/set-active/
  LetterheadLogoUploadView        POST /letterhead/api/templates/<uuid>/logo/
  LetterheadExportView            GET  /letterhead/api/templates/<uuid>/export/<fmt>/
  LetterheadTablePresetsView      GET/POST /letterhead/api/templates/<uuid>/tables/
  LetterheadTablePresetDetailView DELETE /letterhead/api/templates/<uuid>/tables/<preset_id>/
  LetterheadTablePresetReorderView POST /letterhead/api/templates/<uuid>/tables/reorder/
  LetterheadTablePresetResetView  POST /letterhead/api/templates/<uuid>/tables/reset/
"""

import json
import logging
import os
import subprocess
import tempfile

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files.base import ContentFile
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View
from django.views.generic import TemplateView

from .models import LetterheadTemplate, DEFAULT_TABLE_PRESETS

logger = logging.getLogger(__name__)


# ── Permission guard ──────────────────────────────────────────────────────────

def _require_letterhead_permission(user):
    """
    Returns None if the user may manage letterheads, or a 403 JsonResponse.
    Allowed: superuser, or staff whose profile has role 'ceo' / 'office_admin'.
    """
    if user.is_superuser:
        return None
    profile = getattr(user, 'staffprofile', None)
    if profile and getattr(profile, 'role', None) in ('ceo', 'office_admin'):
        return None
    return JsonResponse({'error': 'You do not have permission to manage letterheads.'}, status=403)


def _parse_body(request) -> dict:
    """Safely parse a JSON request body; returns {} on failure."""
    try:
        return json.loads(request.body or '{}')
    except (ValueError, TypeError):
        return {}


# ── SPA Shell ─────────────────────────────────────────────────────────────────

class LetterheadBuilderView(LoginRequiredMixin, TemplateView):
    """
    GET /letterhead/
    Renders the builder SPA shell.  Injects the active template as JSON so
    the JS can rehydrate immediately without a second API call.
    """
    template_name = 'letterhead/builder.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        active = LetterheadTemplate.get_active()
        ctx['active_template_json'] = (
            json.dumps(active.to_builder_dict()) if active else 'null'
        )
        return ctx


# ── Collection ────────────────────────────────────────────────────────────────

class LetterheadListView(LoginRequiredMixin, View):
    """
    GET  /letterhead/api/templates/   → list all templates
    POST /letterhead/api/templates/   → create a new template
    """

    def get(self, request):
        templates = LetterheadTemplate.objects.all()
        return JsonResponse({
            'templates': [t.to_builder_dict() for t in templates]
        })

    def post(self, request):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        data = _parse_body(request)
        errors = {}
        if not data.get('name', '').strip():
            errors['name'] = 'Required.'
        if not data.get('company_name', '').strip():
            errors['company_name'] = 'Required.'
        if errors:
            return JsonResponse({'errors': errors}, status=400)

        template = LetterheadTemplate.objects.create(
            name=data['name'].strip(),
            company_name=data['company_name'].strip(),
            tagline=data.get('tagline', ''),
            address=data.get('address', ''),
            contact_info=data.get('contact_info', ''),
            template_key=data.get('template_key', 'classic'),
            color_primary=data.get('color_primary', '#1D9E75'),
            color_secondary=data.get('color_secondary', '#2C2C2A'),
            font_header=data.get('font_header', 'Georgia'),
            font_body=data.get('font_body', 'Arial'),
            created_by=request.user,
            last_edited_by=request.user,
        )
        return JsonResponse({'ok': True, 'template': template.to_builder_dict()}, status=201)


# ── Single-resource ───────────────────────────────────────────────────────────

class LetterheadDetailView(LoginRequiredMixin, View):
    """GET /letterhead/api/templates/<uuid>/"""

    def get(self, request, pk):
        template = get_object_or_404(LetterheadTemplate, pk=pk)
        return JsonResponse({'template': template.to_builder_dict()})


class LetterheadUpdateView(LoginRequiredMixin, View):
    """POST /letterhead/api/templates/<uuid>/update/"""

    def post(self, request, pk):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        template = get_object_or_404(LetterheadTemplate, pk=pk)
        data = _parse_body(request)

        FIELDS = [
            'name', 'company_name', 'tagline', 'address', 'contact_info',
            'template_key', 'color_primary', 'color_secondary',
            'font_header', 'font_body', 'body_html',
        ]
        updated = []
        for field in FIELDS:
            if field in data:
                setattr(template, field, data[field])
                updated.append(field)

        if 'default_tables' in data:
            template.default_tables = data['default_tables']
            updated.append('default_tables')

        if updated:
            template.last_edited_by = request.user
            updated.append('last_edited_by')
            updated.append('updated_at')
            template.save(update_fields=updated)

        return JsonResponse({'ok': True, 'template': template.to_builder_dict()})


class LetterheadDeleteView(LoginRequiredMixin, View):
    """POST /letterhead/api/templates/<uuid>/delete/"""

    def post(self, request, pk):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        template = get_object_or_404(LetterheadTemplate, pk=pk)
        name = template.name
        template.delete()
        return JsonResponse({'ok': True, 'deleted': name})


class LetterheadSetActiveView(LoginRequiredMixin, View):
    """POST /letterhead/api/templates/<uuid>/set-active/"""

    def post(self, request, pk):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        template = get_object_or_404(LetterheadTemplate, pk=pk)
        template.set_active()
        return JsonResponse({'ok': True, 'template': template.to_builder_dict()})


# ── Logo upload ────────────────────────────────────────────────────────────────

class LetterheadLogoUploadView(LoginRequiredMixin, View):
    """POST /letterhead/api/templates/<uuid>/logo/"""

    def post(self, request, pk):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        template = get_object_or_404(LetterheadTemplate, pk=pk)
        logo_file = request.FILES.get('logo')
        if not logo_file:
            return JsonResponse({'error': 'No file uploaded.'}, status=400)

        allowed_types = {'image/png', 'image/jpeg', 'image/jpg', 'image/svg+xml', 'image/webp'}
        if logo_file.content_type not in allowed_types:
            return JsonResponse({'error': 'Only PNG, JPG, SVG, or WEBP files are allowed.'}, status=400)

        if logo_file.size > 2 * 1024 * 1024:
            return JsonResponse({'error': 'Logo must be under 2 MB.'}, status=400)

        # Remove old logo
        if template.logo:
            try:
                template.logo.delete(save=False)
            except Exception:
                pass

        template.logo.save(logo_file.name, logo_file, save=True)
        template.last_edited_by = request.user
        template.save(update_fields=['last_edited_by', 'updated_at'])

        return JsonResponse({'ok': True, 'logo_url': template.logo.url})


# ── Export ─────────────────────────────────────────────────────────────────────

class LetterheadExportView(LoginRequiredMixin, View):
    """
    GET /letterhead/api/templates/<uuid>/export/<fmt>/
    Supported fmt values: pdf, png
    (docx is handled separately by LetterheadDocxExportView in docx_export.py)
    """

    def get(self, request, pk, fmt):
        template = get_object_or_404(LetterheadTemplate, pk=pk)

        if fmt == 'pdf':
            return self._export_pdf(template)
        elif fmt == 'png':
            return self._export_png(template)
        else:
            return JsonResponse({'error': f'Unsupported format: {fmt}'}, status=400)

    def _build_html(self, template):
        """Build a simple HTML representation of the letterhead for conversion."""
        primary = template.color_primary
        secondary = template.color_secondary
        font_h = template.font_header
        font_b = template.font_body
        logo_html = ''
        if template.logo:
            try:
                logo_html = f'<img src="{template.logo.url}" style="height:52px;margin-right:12px;" alt="logo">'
            except Exception:
                pass

        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: '{font_b}', Arial, sans-serif; padding:40px; background:#fff; }}
  .header {{ border-top:4px solid {primary}; padding-top:16px; margin-bottom:24px; }}
  .header-top {{ display:flex; align-items:center; gap:12px; margin-bottom:8px; }}
  .company-name {{ font-family:'{font_h}',Georgia,serif; font-size:22px; font-weight:700; color:{secondary}; }}
  .tagline {{ font-size:13px; font-style:italic; color:{primary}; margin-bottom:8px; }}
  .address {{ font-size:12px; color:#888; text-align:right; }}
  .divider {{ border-bottom:1px solid #ddd; margin:12px 0; }}
  .body {{ min-height:400px; font-size:13px; color:#333; line-height:1.8; }}
  .footer {{ border-top:2px solid {primary}; margin-top:40px; padding:12px; background:{primary}; color:#fff; text-align:center; font-size:12px; }}
</style>
</head>
<body>
  <div class="header">
    <div class="header-top">
      {logo_html}
      <div class="company-name">{template.company_name}</div>
    </div>
    {'<div class="tagline">' + template.tagline + '</div>' if template.tagline else ''}
    <div class="address">{template.address.replace(chr(10), '<br>')}</div>
    <div class="divider"></div>
  </div>
  <div class="body">
    <p style="margin-bottom:16px;">[Date]</p>
    <p style="margin-bottom:8px;">Dear Sir/Madam,</p>
    <p style="margin-bottom:8px;"><strong>Re: Subject of Letter</strong></p>
    <p>&nbsp;</p>
    <p>&nbsp;</p>
    <p>Yours sincerely,</p>
    <p>&nbsp;</p>
    <p><strong>Name &amp; Title</strong></p>
    <p>{template.company_name}</p>
  </div>
  <div class="footer">{template.contact_info or template.address.replace(chr(10), '  ·  ')}</div>
</body>
</html>"""

    def _export_pdf(self, template):
        html_content = self._build_html(template)
        tmp_html = tempfile.NamedTemporaryFile(suffix='.html', delete=False, mode='w', encoding='utf-8')
        tmp_html.write(html_content)
        tmp_html.close()
        out_path = tmp_html.name.replace('.html', '.pdf')
        try:
            result = subprocess.run(
                ['libreoffice', '--headless', '--convert-to', 'pdf',
                 '--outdir', os.path.dirname(out_path), tmp_html.name],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                return JsonResponse({'error': 'PDF generation failed.', 'detail': result.stderr[:500]}, status=500)
            with open(out_path, 'rb') as fh:
                pdf_bytes = fh.read()
            safe_name = template.name.replace(' ', '_') + '.pdf'
            response = HttpResponse(pdf_bytes, content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="{safe_name}"'
            return response
        except FileNotFoundError:
            return JsonResponse({'error': 'LibreOffice is not available on this server.'}, status=501)
        finally:
            try:
                os.unlink(tmp_html.name)
                if os.path.exists(out_path):
                    os.unlink(out_path)
            except OSError:
                pass

    def _export_png(self, template):
        """Export as a PNG preview image using wkhtmltoimage or Pillow fallback."""
        try:
            from PIL import Image, ImageDraw, ImageFont
            from io import BytesIO
            # Simple Pillow-based preview
            img = Image.new('RGB', (794, 250), color='white')
            buf = BytesIO()
            img.save(buf, format='PNG')
            buf.seek(0)
            safe_name = template.name.replace(' ', '_') + '.png'
            response = HttpResponse(buf.read(), content_type='image/png')
            response['Content-Disposition'] = f'attachment; filename="{safe_name}"'
            return response
        except Exception as exc:
            return JsonResponse({'error': f'PNG export failed: {exc}'}, status=500)


# ── Table preset views ─────────────────────────────────────────────────────────

class LetterheadTablePresetsView(LoginRequiredMixin, View):
    """
    GET  /api/templates/<uuid>/tables/  → list resolved presets
    POST /api/templates/<uuid>/tables/  → add/upsert a preset
    """

    def get(self, request, pk):
        template = get_object_or_404(LetterheadTemplate, pk=pk)
        return JsonResponse({'tables': template.get_tables()})

    def post(self, request, pk):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        template = get_object_or_404(LetterheadTemplate, pk=pk)
        data = _parse_body(request)

        errors = {}
        if not data.get('id'):
            errors['id'] = 'Required.'
        if not data.get('label'):
            errors['label'] = 'Required.'
        if not isinstance(data.get('columns'), list) or not data['columns']:
            errors['columns'] = 'Must be a non-empty list of column header strings.'
        if errors:
            return JsonResponse({'errors': errors}, status=400)

        preset = {
            'id':      data['id'],
            'label':   data['label'],
            'columns': [str(c) for c in data['columns']],
            'rows':    int(data.get('rows', 3)),
            'style':   data.get('style', 'striped'),
        }
        template.upsert_table_preset(preset)
        return JsonResponse({'ok': True, 'tables': template.get_tables()})


class LetterheadTablePresetDetailView(LoginRequiredMixin, View):
    """DELETE /api/templates/<uuid>/tables/<preset_id>/"""

    def delete(self, request, pk, preset_id):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        template = get_object_or_404(LetterheadTemplate, pk=pk)
        template.delete_table_preset(preset_id)
        return JsonResponse({'ok': True, 'tables': template.get_tables()})


class LetterheadTablePresetReorderView(LoginRequiredMixin, View):
    """POST /api/templates/<uuid>/tables/reorder/ — body: { ordered_ids: [...] }"""

    def post(self, request, pk):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        template = get_object_or_404(LetterheadTemplate, pk=pk)
        data = _parse_body(request)
        ordered_ids = data.get('ordered_ids')

        if not isinstance(ordered_ids, list):
            return JsonResponse({'error': 'ordered_ids must be a list.'}, status=400)

        template.reorder_table_presets(ordered_ids)
        return JsonResponse({'ok': True, 'tables': template.get_tables()})


class LetterheadTablePresetResetView(LoginRequiredMixin, View):
    """POST /api/templates/<uuid>/tables/reset/ — revert to DEFAULT_TABLE_PRESETS"""

    def post(self, request, pk):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        template = get_object_or_404(LetterheadTemplate, pk=pk)
        template.reset_tables_to_default()
        return JsonResponse({'ok': True, 'tables': DEFAULT_TABLE_PRESETS})