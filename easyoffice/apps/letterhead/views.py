"""
apps/letterhead/views.py

Who can use this tool
─────────────────────
  • Django superusers
  • Users whose profile has role == 'ceo'
  • Users whose profile has role == 'office_admin'

Everyone else receives 403.  The helper _can_manage_letterhead() encodes
this rule once; all views call it.

View map
─────────
  LetterheadBuilderView   GET  /letterhead/                 Builder SPA shell
  LetterheadListView      GET  /letterhead/api/templates/   JSON list
  LetterheadCreateView    POST /letterhead/api/templates/   Create new
  LetterheadDetailView    GET  /letterhead/api/templates/<uuid>/   Single record
  LetterheadUpdateView    PUT  /letterhead/api/templates/<uuid>/   Full update
  LetterheadDeleteView    DELETE /letterhead/api/templates/<uuid>/ Delete
  LetterheadSetActiveView POST /letterhead/api/templates/<uuid>/set-active/
  LetterheadLogoUploadView POST /letterhead/api/templates/<uuid>/logo/
  LetterheadExportView    GET  /letterhead/api/templates/<uuid>/export/<fmt>/
                              fmt ∈ {png, pdf}  — server-side rendering
                              (Word export is handled entirely client-side)
"""
import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files.storage import default_storage
from django.http import (
    JsonResponse, HttpResponseForbidden, HttpResponseBadRequest,
    HttpResponse,
)
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import View

from .models import LetterheadTemplate

logger = logging.getLogger(__name__)


# ── Permission gate ───────────────────────────────────────────────────────────

def _can_manage_letterhead(user):
    """
    Returns True if the user is allowed to create / edit letterheads.

    Checks:
      1. Django superuser flag — always granted.
      2. A 'role' attribute on the user's profile (adapt the attribute
         name to match your AUTH_USER_MODEL / profile model).
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    # Adapt to your user/profile model.  Common patterns:
    #   user.role  (field directly on custom user model)
    #   user.profile.role
    role = getattr(user, 'role', None) or getattr(
        getattr(user, 'profile', None), 'role', None
    )
    return role in ('ceo', 'office_admin')


def _require_letterhead_permission(user):
    """Return None if allowed, else an HttpResponseForbidden."""
    if _can_manage_letterhead(user):
        return None
    return HttpResponseForbidden(
        json.dumps({'error': 'Only superusers, the CEO, and Office Admins may manage letterheads.'}),
        content_type='application/json',
    )


# ── Builder SPA shell ─────────────────────────────────────────────────────────

class LetterheadBuilderView(LoginRequiredMixin, View):
    """
    GET /letterhead/

    Renders the single-page builder.  Passes the active template (if any)
    as JSON into the template context so the JS can pre-populate the form.
    """
    template_name = 'letterhead/builder.html'

    def get(self, request):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        active = LetterheadTemplate.get_active()
        ctx = {
            'active_template_json': json.dumps(
                active.to_builder_dict() if active else None
            ),
        }
        return TemplateResponse(request, self.template_name, ctx)


# ── REST-style JSON API ───────────────────────────────────────────────────────

@method_decorator(csrf_exempt, name='dispatch')
class LetterheadListView(LoginRequiredMixin, View):
    """
    GET  /letterhead/api/templates/   → list all
    POST /letterhead/api/templates/   → create new
    """

    def get(self, request):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        templates = LetterheadTemplate.objects.all()
        return JsonResponse({
            'templates': [t.to_builder_dict() for t in templates]
        })

    def post(self, request):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        try:
            data = json.loads(request.body or b'{}')
        except ValueError:
            return HttpResponseBadRequest(
                json.dumps({'error': 'Invalid JSON.'}),
                content_type='application/json',
            )

        errors = _validate_payload(data)
        if errors:
            return JsonResponse({'errors': errors}, status=400)

        template = LetterheadTemplate(
            created_by=request.user,
            last_edited_by=request.user,
        )
        _apply_payload(template, data)
        template.save()

        return JsonResponse(template.to_builder_dict(), status=201)


@method_decorator(csrf_exempt, name='dispatch')
class LetterheadDetailView(LoginRequiredMixin, View):
    """GET /letterhead/api/templates/<uuid>/"""

    def get(self, request, pk):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        template = get_object_or_404(LetterheadTemplate, pk=pk)
        return JsonResponse(template.to_builder_dict())


@method_decorator(csrf_exempt, name='dispatch')
class LetterheadUpdateView(LoginRequiredMixin, View):
    """PUT /letterhead/api/templates/<uuid>/"""

    def put(self, request, pk):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        template = get_object_or_404(LetterheadTemplate, pk=pk)

        try:
            data = json.loads(request.body or b'{}')
        except ValueError:
            return HttpResponseBadRequest(
                json.dumps({'error': 'Invalid JSON.'}),
                content_type='application/json',
            )

        errors = _validate_payload(data)
        if errors:
            return JsonResponse({'errors': errors}, status=400)

        _apply_payload(template, data)
        template.last_edited_by = request.user
        template.save()

        return JsonResponse(template.to_builder_dict())


@method_decorator(csrf_exempt, name='dispatch')
class LetterheadDeleteView(LoginRequiredMixin, View):
    """DELETE /letterhead/api/templates/<uuid>/"""

    def delete(self, request, pk):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        template = get_object_or_404(LetterheadTemplate, pk=pk)

        if template.is_active:
            return JsonResponse(
                {'error': 'Cannot delete the active letterhead. Set another as active first.'},
                status=409,
            )

        # Remove logo file from storage if present.
        if template.logo:
            try:
                default_storage.delete(template.logo.name)
            except Exception as exc:
                logger.warning('Could not delete logo file %s: %s', template.logo.name, exc)

        template.delete()
        return JsonResponse({'ok': True})


@method_decorator(csrf_exempt, name='dispatch')
class LetterheadSetActiveView(LoginRequiredMixin, View):
    """POST /letterhead/api/templates/<uuid>/set-active/"""

    def post(self, request, pk):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        template = get_object_or_404(LetterheadTemplate, pk=pk)
        template.set_active()
        return JsonResponse({'ok': True, 'active_id': str(template.pk)})


@method_decorator(csrf_exempt, name='dispatch')
class LetterheadLogoUploadView(LoginRequiredMixin, View):
    """
    POST /letterhead/api/templates/<uuid>/logo/
    Content-Type: multipart/form-data
    Field:  logo  (file)
    """

    def post(self, request, pk):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        template = get_object_or_404(LetterheadTemplate, pk=pk)

        logo_file = request.FILES.get('logo')
        if not logo_file:
            return HttpResponseBadRequest(
                json.dumps({'error': 'No file uploaded.  Send a multipart field named "logo".'}),
                content_type='application/json',
            )

        allowed_types = {'image/png', 'image/jpeg', 'image/jpg', 'image/svg+xml', 'image/webp'}
        if logo_file.content_type not in allowed_types:
            return JsonResponse(
                {'error': f'Unsupported file type: {logo_file.content_type}. Use PNG, JPG, or SVG.'},
                status=415,
            )

        # Remove old logo file before saving the new one.
        if template.logo:
            try:
                default_storage.delete(template.logo.name)
            except Exception as exc:
                logger.warning('Could not delete old logo: %s', exc)

        template.logo = logo_file
        template.last_edited_by = request.user
        template.save(update_fields=['logo', 'last_edited_by', 'updated_at'])

        return JsonResponse({
            'ok': True,
            'logo_url': template.logo.url,
        })


class LetterheadExportView(LoginRequiredMixin, View):
    """
    GET /letterhead/api/templates/<uuid>/export/<fmt>/
    fmt: png | pdf

    Server-side export using Pillow (PNG) or ReportLab (PDF).
    Falls back gracefully with a clear error if the library is missing.

    Word (.docx) export is done entirely in the browser (JS canvas → blob)
    and does NOT hit this endpoint.
    """

    def get(self, request, pk, fmt):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        fmt = fmt.lower()
        if fmt not in ('png', 'pdf'):
            return HttpResponseBadRequest('Supported formats: png, pdf')

        template = get_object_or_404(LetterheadTemplate, pk=pk)

        if fmt == 'png':
            return self._export_png(template)
        return self._export_pdf(template)

    # ── PNG via Pillow ────────────────────────────────────────────────────

    def _export_png(self, template):
        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            return HttpResponse(
                'Pillow is required for PNG export.  Install it: pip install Pillow',
                status=501,
                content_type='text/plain',
            )

        # A4 at 150 DPI → 1240 × 1754 px  (use 300 DPI for print: 2480 × 3508)
        DPI = 150
        W, H = int(8.27 * DPI), int(11.69 * DPI)
        img = Image.new('RGB', (W, H), color='white')
        draw = ImageDraw.Draw(img)

        primary   = template.color_primary
        secondary = template.color_secondary

        def hex_to_rgb(h):
            h = h.lstrip('#')
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

        p_rgb = hex_to_rgb(primary)
        s_rgb = hex_to_rgb(secondary)

        # Simple "classic" layout regardless of template_key for the
        # server-side render.  A full implementation would mirror each
        # JS template in Python — use the JS canvas export for pixel-
        # perfect results; this endpoint is a server-side fallback.
        margin = 60

        # Top accent bar
        draw.rectangle([0, 0, W, 18], fill=p_rgb)

        # Company name
        try:
            font_large = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf', 52)
            font_small = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 26)
        except OSError:
            font_large = ImageFont.load_default()
            font_small = ImageFont.load_default()

        draw.text((margin, 60), template.company_name, font=font_large, fill=s_rgb)
        if template.tagline:
            draw.text((margin, 130), template.tagline, font=font_small, fill=p_rgb)

        # Divider
        draw.rectangle([margin, 175, W - margin, 177], fill=(220, 220, 220))

        # Address + contact
        y = 195
        for line in (template.address or '').split('\n'):
            draw.text((margin, y), line.strip(), font=font_small, fill=(120, 120, 120))
            y += 36
        draw.text((margin, y + 10), template.contact_info or '', font=font_small, fill=(150, 150, 150))

        # Footer bar
        draw.rectangle([0, H - 130, W, H], fill=p_rgb)
        draw.text(
            (W // 2 - 200, H - 80),
            (template.contact_info or ''),
            font=font_small, fill=(255, 255, 255),
        )

        # Logo overlay (top-right)
        if template.logo:
            try:
                logo = Image.open(template.logo.path).convert('RGBA')
                logo.thumbnail((300, 150), Image.LANCZOS)
                img.paste(logo, (W - margin - logo.width, 50), logo)
            except Exception as exc:
                logger.warning('Could not embed logo in PNG export: %s', exc)

        from io import BytesIO
        buf = BytesIO()
        img.save(buf, format='PNG', dpi=(DPI, DPI))
        buf.seek(0)

        filename = f'letterhead_{template.name.replace(" ", "_")}.png'
        response = HttpResponse(buf.read(), content_type='image/png')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response

    # ── PDF via ReportLab ──────────────────────────────────────────────────

    def _export_pdf(self, template):
        try:
            from reportlab.pdfgen import canvas as rl_canvas
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.colors import HexColor
        except ImportError:
            return HttpResponse(
                'ReportLab is required for PDF export.  Install it: pip install reportlab',
                status=501,
                content_type='text/plain',
            )

        from io import BytesIO

        buf = BytesIO()
        W, H = A4   # points (595 × 842)
        c = rl_canvas.Canvas(buf, pagesize=A4)

        primary   = HexColor(template.color_primary)
        secondary = HexColor(template.color_secondary)
        margin = 40

        # Top accent bar
        c.setFillColor(primary)
        c.rect(0, H - 6, W, 6, fill=1, stroke=0)

        # Company name
        c.setFillColor(secondary)
        c.setFont('Helvetica-Bold', 20)
        c.drawString(margin, H - 50, template.company_name)

        if template.tagline:
            c.setFillColor(primary)
            c.setFont('Helvetica-Oblique', 9)
            c.drawString(margin, H - 64, template.tagline)

        # Divider
        c.setStrokeColorRGB(0.85, 0.85, 0.85)
        c.setLineWidth(0.5)
        c.line(margin, H - 78, W - margin, H - 78)

        # Address & contact (right-aligned)
        c.setFillColorRGB(0.55, 0.55, 0.55)
        c.setFont('Helvetica', 8)
        addr_lines = (template.address or '').split('\n')
        y = H - 42
        for line in addr_lines:
            c.drawRightString(W - margin, y, line.strip())
            y -= 12
        c.drawRightString(W - margin, y, template.contact_info or '')

        # Logo
        if template.logo:
            try:
                from reportlab.lib.utils import ImageReader
                logo_img = ImageReader(template.logo.path)
                iw, ih = logo_img.getSize()
                max_h = 40
                ratio = max_h / ih
                c.drawImage(
                    logo_img,
                    W - margin - iw * ratio - 5,
                    H - 6 - max_h - 5,
                    width=iw * ratio, height=max_h,
                    mask='auto',
                )
            except Exception as exc:
                logger.warning('Could not embed logo in PDF: %s', exc)

        # Footer
        c.setFillColor(primary)
        c.rect(0, 0, W, 30, fill=1, stroke=0)
        c.setFillColorRGB(1, 1, 1)
        c.setFont('Helvetica', 8)
        footer = f'{template.contact_info}   ·   {(template.address or "").replace(chr(10), " ")}'
        c.drawCentredString(W / 2, 11, footer)

        c.showPage()
        c.save()
        buf.seek(0)

        filename = f'letterhead_{template.name.replace(" ", "_")}.pdf'
        response = HttpResponse(buf.read(), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


# ── Payload helpers ───────────────────────────────────────────────────────────

_HEX_RE = __import__('re').compile(r'^#[0-9A-Fa-f]{6}$')

VALID_TEMPLATES = {c[0] for c in LetterheadTemplate._meta.get_field('template_key').choices}
VALID_FONTS     = {c[0] for c in LetterheadTemplate._meta.get_field('font_header').choices}


def _validate_payload(data: dict) -> list:
    errors = []
    if not data.get('name', '').strip():
        errors.append('name is required.')
    if not data.get('company_name', '').strip():
        errors.append('company_name is required.')
    if data.get('template_key') and data['template_key'] not in VALID_TEMPLATES:
        errors.append(f"template_key must be one of: {', '.join(sorted(VALID_TEMPLATES))}")
    for field in ('color_primary', 'color_secondary'):
        val = data.get(field, '')
        if val and not _HEX_RE.match(val):
            errors.append(f'{field} must be a 6-digit hex colour like #1D9E75.')
    return errors


def _apply_payload(template: LetterheadTemplate, data: dict):
    """Write validated payload fields onto a model instance (unsaved)."""
    template.name          = data.get('name', template.name).strip()
    template.template_key  = data.get('template_key',   template.template_key)
    template.company_name  = data.get('company_name',   template.company_name).strip()
    template.tagline       = data.get('tagline',        template.tagline or '').strip()
    template.address       = data.get('address',        template.address or '').strip()
    template.contact_info  = data.get('contact_info',   template.contact_info or '').strip()
    template.color_primary   = data.get('color_primary',   template.color_primary)
    template.color_secondary = data.get('color_secondary', template.color_secondary)
    template.font_header   = data.get('font_header',    template.font_header)
    template.font_body     = data.get('font_body',      template.font_body)
