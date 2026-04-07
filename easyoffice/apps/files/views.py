import os, subprocess, tempfile, hashlib
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, View
from django.shortcuts import render, redirect, get_object_or_404
from django.http import FileResponse, Http404, JsonResponse
from django.contrib import messages
from django.utils import timezone
from django.db.models import Q, Sum
from django.core.mail import send_mail
from django.conf import settings
import os, subprocess, tempfile, hashlib, mimetypes, re
from django.utils.decorators import method_decorator
from io import BytesIO
from django.core.files.base import ContentFile
from pypdf import PdfReader, PdfWriter
from PIL import Image
from django.views.decorators.clickjacking import xframe_options_sameorigin
from apps.files.models import (
    SharedFile, FileFolder, SignatureRequest,
    SignatureRequestSigner, SignatureAuditEvent, CONVERTIBLE_TYPES
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_client_ip(request):
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _visible_files_qs(user):
    profile = getattr(user, 'staffprofile', None)
    unit = profile.unit if profile else None
    dept = profile.department if profile else None
    return SharedFile.objects.filter(
        Q(uploaded_by=user) |
        Q(visibility='office') |
        Q(visibility='unit', unit=unit) |
        Q(visibility='department', department=dept) |
        Q(shared_with=user) |
        Q(share_access__user=user)
    ).distinct()


def _visible_folders_qs(user):
    profile = getattr(user, 'staffprofile', None)
    unit = profile.unit if profile else None
    dept = profile.department if profile else None
    return FileFolder.objects.filter(
        Q(owner=user) |
        Q(visibility='office') |
        Q(visibility='unit', unit=unit) |
        Q(visibility='department', department=dept) |
        Q(share_access__user=user)
    ).distinct()

def _file_permission_for(user, f):
    if f.uploaded_by_id == user.id or user.is_superuser:
        return 'full'

    access = f.share_access.filter(user=user).first()
    if access:
        return access.permission

    profile = getattr(user, 'staffprofile', None)
    unit = profile.unit if profile else None
    dept = profile.department if profile else None

    if f.visibility == 'office':
        return 'view'
    if f.visibility == 'unit' and f.unit == unit:
        return 'view'
    if f.visibility == 'department' and f.department == dept:
        return 'view'
    if f.shared_with.filter(id=user.id).exists():
        return 'view'

    return None


def _folder_permission_for(user, folder):
    if folder.owner_id == user.id or user.is_superuser:
        return 'full'

    access = folder.share_access.filter(user=user).first()
    if access:
        return access.permission

    profile = getattr(user, 'staffprofile', None)
    unit = profile.unit if profile else None
    dept = profile.department if profile else None

    if folder.visibility == 'office':
        return 'view'
    if folder.visibility == 'unit' and folder.unit == unit:
        return 'view'
    if folder.visibility == 'department' and folder.department == dept:
        return 'view'

    return None


def _can_edit_file(user, f):
    perm = _file_permission_for(user, f)
    return perm in ('edit', 'full')


def _can_delete_file(user, f):
    perm = _file_permission_for(user, f)
    return perm == 'full'


def _can_edit_folder(user, folder):
    perm = _folder_permission_for(user, folder)
    return perm in ('edit', 'full')


def _can_delete_folder(user, folder):
    perm = _folder_permission_for(user, folder)
    return perm == 'full'


def _log_file_history(file_obj, action, actor=None, notes=''):
    try:
        FileHistory.objects.create(
            file=file_obj,
            action=action,
            actor=actor,
            notes=notes,
            snapshot_name=file_obj.name or '',
            snapshot_folder_name=file_obj.folder.name if file_obj.folder else '',
            snapshot_visibility=file_obj.visibility or '',
        )
    except Exception:
        pass

def _log_audit(sig_req, event, signer=None, request=None, notes=''):
    """Create a tamper-evident audit event."""
    SignatureAuditEvent.objects.create(
        request=sig_req,
        event=event,
        signer_email=signer.email if signer else '',
        signer_name=signer.name if signer else '',
        ip_address=_get_client_ip(request) if request else None,
        user_agent=request.META.get('HTTP_USER_AGENT', '')[:500] if request else '',
        notes=notes,
    )
    sig_req.rebuild_audit_hash()


def _convert_to_pdf(source_path):
    """
    Convert a document to PDF using LibreOffice headless.
    Returns path to generated PDF, or raises RuntimeError.
    """
    out_dir = tempfile.mkdtemp()
    try:
        result = subprocess.run(
            ['libreoffice', '--headless', '--convert-to', 'pdf',
             '--outdir', out_dir, source_path],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            raise RuntimeError(f'LibreOffice error: {result.stderr}')
        base = os.path.splitext(os.path.basename(source_path))[0]
        pdf_path = os.path.join(out_dir, base + '.pdf')
        if not os.path.exists(pdf_path):
            raise RuntimeError('PDF output not found after conversion')
        return pdf_path
    except FileNotFoundError:
        raise RuntimeError(
            'LibreOffice is not installed. Install with: apt-get install libreoffice'
        )

def _convert_image_to_pdf(source_path):
    """
    Convert an image file to PDF using Pillow.
    Returns bytes of the generated PDF.
    """
    with Image.open(source_path) as img:
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        buffer = BytesIO()
        img.save(buffer, format="PDF", resolution=100.0)
        buffer.seek(0)
        return buffer

def _notify_signer(signer, base_url):
    """Send an email notification to a signer."""
    sign_url = base_url.rstrip('/') + signer.signing_url
    try:
        send_mail(
            subject=f'Please sign: {signer.request.title}',
            message=(
                f'Hello {signer.name},\n\n'
                f'{signer.request.created_by.full_name} has requested your digital signature on:\n'
                f'"{signer.request.title}"\n\n'
                f'{signer.request.message}\n\n'
                f'Click the link below to review and sign the document:\n{sign_url}\n\n'
                f'This link is unique to you. Do not share it.\n\n'
                f'— EasyOffice'
            ),
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@easyoffice.local'),
            recipient_list=[signer.email],
            fail_silently=True,
        )
    except Exception:
        pass


def _push_notification(user, notif_type, title, body='', link='', icon='bi-bell-fill', color='#3b82f6'):
    """
    Push a real-time notification to a user via the existing
    notifications_ws WebSocket channel (group: notifications_{user.id}).
    Silently skips if Channels / Redis is not running.
    """
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        if layer is None:
            return
        async_to_sync(layer.group_send)(
            f'notifications_{user.id}',
            {
                'type': 'send_notification',   # handled by NotificationConsumer.send_notification
                'data': {
                    'type':  notif_type,
                    'title': title,
                    'body':  body,
                    'link':  link,
                    'icon':  icon,
                    'color': color,
                },
            }
        )
    except Exception:
        pass

def _store_notification(recipient, sender, notif_type, title, body='', link=''):
    """
    Store a persistent in-app notification for the main notification bell.
    Mirrors what the meetings app is doing with CoreNotification.
    """
    try:
        from apps.core.models import CoreNotification
        CoreNotification.objects.create(
            recipient=recipient,
            sender=sender,
            notification_type=notif_type,
            title=title,
            message=body,
            link=link,
        )
    except Exception:
        pass

# Notification type → (icon, colour) — mirrors the consumer's expected payload
_NOTIF_META = {
    'file_shared':    ('bi-share-fill',         '#3b82f6'),
    'sign_request':   ('bi-pen-fill',           '#8b5cf6'),
    'sign_viewed':    ('bi-eye-fill',           '#06b6d4'),
    'sign_signed':    ('bi-check-circle-fill',  '#10b981'),
    'sign_declined':  ('bi-x-circle-fill',      '#ef4444'),
    'sign_completed': ('bi-patch-check-fill',   '#10b981'),
    'sign_reminder':  ('bi-alarm-fill',         '#f59e0b'),
}


def _notify(user, notif_type, title, body='', link='', sender=None):
    """
    Send both:
    1. real-time websocket notification
    2. persistent database notification for the main bell
    """
    icon, color = _NOTIF_META.get(notif_type, ('bi-bell-fill', '#64748b'))

    # Real-time / websocket
    _push_notification(user, notif_type, title, body, link, icon, color)

    # Persistent / main bell
    if sender is not None:
        _store_notification(
            recipient=user,
            sender=sender,
            notif_type=notif_type,
            title=title,
            body=body,
            link=link,
        )


def _get_sig_font_path(font_name='Dancing Script'):
    """
    Download and cache the TTF for the requested signature font.
    Falls back through a chain of system fonts if download fails.
    Returns the local path, or None if all attempts fail.
    """
    import urllib.request

    # Map UI font names → (cache filename, GitHub raw URL)
    _REGISTRY = {
        'Dancing Script': (
            '_eo_DancingScript_Bold.ttf',
            'https://github.com/googlefonts/dancing-script/raw/main/fonts/ttf/DancingScript-Bold.ttf',
        ),
        'Caveat': (
            '_eo_Caveat_Bold.ttf',
            'https://github.com/googlefonts/caveat/raw/main/fonts/ttf/Caveat-Bold.ttf',
        ),
        'Pacifico': (
            '_eo_Pacifico_Regular.ttf',
            'https://github.com/google/fonts/raw/main/ofl/pacifico/Pacifico-Regular.ttf',
        ),
        'Great Vibes': (
            '_eo_GreatVibes_Regular.ttf',
            'https://github.com/google/fonts/raw/main/ofl/greatvibes/GreatVibes-Regular.ttf',
        ),
    }

    cache_file, url = _REGISTRY.get(font_name, _REGISTRY['Dancing Script'])
    cache_path = os.path.join(tempfile.gettempdir(), cache_file)

    if os.path.exists(cache_path):
        return cache_path

    try:
        urllib.request.urlretrieve(url, cache_path)
        return cache_path
    except Exception:
        pass

    # System font fallbacks (common on Ubuntu/Debian servers)
    for p in [
        '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf',
        '/usr/share/fonts/truetype/freefont/FreeSerifItalic.ttf',
        '/usr/share/fonts/truetype/urw-base35/URWBookman-LightItalic.ttf',
    ]:
        if os.path.exists(p):
            return p

    return None


def _typed_sig_to_image(text, width_pt, height_pt, font_name='Dancing Script', dpi=150):
    """
    Renders a typed signature as a transparent-background PNG using PIL.
    Returns BytesIO of PNG or None on failure.
    font_name is a hint only — we always use the best available font.
    """
    from PIL import ImageFont, ImageDraw
    w_px = max(10, int(width_pt  * dpi / 72))
    h_px = max(10, int(height_pt * dpi / 72))

    font_path  = _get_sig_font_path(font_name)
    font_size  = max(12, int(h_px * 0.60))
    pil_font   = None

    if font_path:
        for attempt in range(3):
            try:
                pil_font = ImageFont.truetype(font_path, font_size)
                break
            except Exception:
                font_size = max(8, font_size - 4)

    if pil_font is None:
        return None   # caller will fall back to reportlab text

    img  = Image.new('RGBA', (w_px, h_px), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    ink  = (16, 69, 197, 240)   # dark DocuSign blue, slight transparency

    # Centre the text
    try:
        bbox = draw.textbbox((0, 0), text, font=pil_font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except AttributeError:
        tw, th = draw.textsize(text, font=pil_font)   # PIL < 9.2

    # Scale down if text wider than image
    if tw > w_px - 8:
        scale = (w_px - 8) / tw
        new_size = max(8, int(font_size * scale))
        try:
            pil_font = ImageFont.truetype(font_path, new_size)
            bbox = draw.textbbox((0, 0), text, font=pil_font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            pass

    x = max(4, (w_px - tw) // 2)
    y = max(0, (h_px - th) // 2 - int(h_px * 0.05))  # slight upward nudge
    draw.text((x, y), text, fill=ink, font=pil_font)

    buf = BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


def _embed_signatures_in_pdf(sig_req):
    """
    Burns every filled SignatureField into the PDF with:
      • DocuSign-style signature field rendering (image or typed text)
      • Tamper-evident header strip on every page  (document ID, title)
      • Tamper-evident footer strip on every page  (SHA-256 hash, timestamp)

    Requirements: pip install reportlab
    """
    try:
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.lib.utils import ImageReader
    except ImportError:
        return None

    import base64
    from collections import defaultdict
    from datetime import datetime as _dt

    document = sig_req.document
    if not document or not getattr(document, 'file', None):
        return None

    from apps.files.models import SignatureField
    fields = list(
        SignatureField.objects
        .filter(request=sig_req)
        .exclude(value='')
        .select_related('signer')
    )
    # We still add header/footer even if no fields
    fields_by_page = defaultdict(list)
    for f in fields:
        fields_by_page[f.page].append(f)

    try:
        pdf_bytes = document.file.open('rb').read()
        reader    = PdfReader(BytesIO(pdf_bytes))
    except Exception:
        return None

    # ── Colour palette ────────────────────────────────────────────────────────
    DS_BLUE        = (0.098, 0.376, 0.875)   # #1960DF
    DS_BLUE_LIGHT  = (0.918, 0.937, 0.992)   # field background
    DS_DATE_BG     = (0.953, 0.961, 0.996)
    DS_INK         = (0.063, 0.271, 0.773)   # #1045C5
    DS_LABEL       = (0.373, 0.420, 0.510)
    DS_CHECK       = (0.063, 0.271, 0.773)

    # ── Header / footer metadata ──────────────────────────────────────────────
    doc_hash   = (document.file_hash or 'N/A')
    short_hash = doc_hash[:16] + '…' if len(doc_hash) > 16 else doc_hash
    full_hash  = doc_hash
    signed_at  = _dt.now().strftime('%d %b %Y %H:%M UTC')
    req_id     = str(sig_req.id)[:8].upper()
    doc_title  = (sig_req.title or document.name or '')[:60]
    STRIP_H    = 18.0   # height of header/footer strip in PDF points

    writer = PdfWriter()

    for page_idx in range(len(reader.pages)):
        page     = reader.pages[page_idx]
        page_num = page_idx + 1
        page_w   = float(page.mediabox.width)
        page_h   = float(page.mediabox.height)
        total_pg = len(reader.pages)

        overlay_buf = BytesIO()
        c = rl_canvas.Canvas(overlay_buf, pagesize=(page_w, page_h))

        # ══════════════════════════════════════════════════════════════════════
        # HEADER STRIP  (top of page)
        # ══════════════════════════════════════════════════════════════════════
        hdr_y = page_h - STRIP_H   # bottom-left Y of header strip

        # Background
        c.setFillColorRGB(0.937, 0.953, 1.0)     # very light blue-white
        c.setStrokeColorRGB(*DS_BLUE)
        c.setLineWidth(0.4)
        c.rect(0, hdr_y, page_w, STRIP_H, stroke=0, fill=1)

        # Bottom border line of header
        c.setStrokeColorRGB(*DS_BLUE)
        c.setLineWidth(0.8)
        c.line(0, hdr_y, page_w, hdr_y)

        # Left: shield icon placeholder + "EasyOffice · Signed Document"
        lbl_y = hdr_y + STRIP_H * 0.28
        c.setFont('Helvetica-Bold', 6.5)
        c.setFillColorRGB(*DS_BLUE)
        c.drawString(5, lbl_y, '\u26BF EasyOffice')   # ⚿ lock-like char

        c.setFont('Helvetica', 6.0)
        c.setFillColorRGB(*DS_LABEL)
        c.drawString(52, lbl_y, f'\u00B7  {doc_title}')

        # Right: "Page N / Total · Req: XXXXXXXX"
        c.setFont('Helvetica', 6.0)
        c.setFillColorRGB(*DS_LABEL)
        right_text = f'Req: {req_id}   \u2022   Page {page_num} / {total_pg}'
        c.drawRightString(page_w - 5, lbl_y, right_text)

        # ══════════════════════════════════════════════════════════════════════
        # FOOTER STRIP  (bottom of page)
        # ══════════════════════════════════════════════════════════════════════
        ftr_y = 0.0

        # Background
        c.setFillColorRGB(0.937, 0.953, 1.0)
        c.rect(0, ftr_y, page_w, STRIP_H, stroke=0, fill=1)

        # Top border line of footer
        c.setStrokeColorRGB(*DS_BLUE)
        c.setLineWidth(0.8)
        c.line(0, STRIP_H, page_w, STRIP_H)

        ftr_lbl_y = ftr_y + STRIP_H * 0.28

        # Left: hash
        c.setFont('Helvetica-Bold', 5.5)
        c.setFillColorRGB(*DS_BLUE)
        c.drawString(5, ftr_lbl_y, 'SHA-256:')
        c.setFont('Courier', 5.5)
        c.setFillColorRGB(0.2, 0.2, 0.2)
        c.drawString(35, ftr_lbl_y, full_hash[:48] + ('…' if len(full_hash) > 48 else ''))

        # Right: signed-at timestamp
        c.setFont('Helvetica', 5.5)
        c.setFillColorRGB(*DS_LABEL)
        c.drawRightString(page_w - 5, ftr_lbl_y, f'Electronically signed \u00B7 {signed_at}')

        # ══════════════════════════════════════════════════════════════════════
        # SIGNATURE FIELDS
        # ══════════════════════════════════════════════════════════════════════
        for field in fields_by_page.get(page_num, []):
            value = field.value.strip()
            if not value:
                continue

            # Coordinate conversion (% → pt, flip Y axis)
            fx = (field.x_pct      / 100.0) * page_w
            fw = (field.width_pct  / 100.0) * page_w
            fh = (field.height_pct / 100.0) * page_h
            fy = page_h * (1.0 - (field.y_pct + field.height_pct) / 100.0)

            is_sig  = field.field_type in ('signature', 'initials')
            is_date = field.field_type == 'date'

            label_h = max(9.0, min(fh * 0.24, 16.0)) if fh >= 20 else 0.0
            sig_h   = fh - label_h
            sig_y   = fy + label_h

            # ── Background + border ──────────────────────────────────────────
            bg = DS_BLUE_LIGHT if is_sig else DS_DATE_BG
            c.setFillColorRGB(*bg)
            c.setStrokeColorRGB(*DS_BLUE)
            c.setLineWidth(0.6)
            c.roundRect(fx, fy, fw, fh, radius=2, stroke=1, fill=1)

            if label_h:
                c.setStrokeColorRGB(*DS_BLUE)
                c.setLineWidth(1.0)
                c.line(fx + 2, fy + label_h, fx + fw - 2, fy + label_h)

            # ── Drawn / uploaded (base64 PNG) ────────────────────────────────
            if is_sig and value.startswith('data:image'):
                try:
                    _, b64 = value.split(',', 1)
                    raw    = base64.b64decode(b64)
                    img    = Image.open(BytesIO(raw)).convert('RGBA')
                    bg_img = Image.new('RGBA', img.size, (255, 255, 255, 255))
                    bg_img.paste(img, mask=img)
                    bg_img = bg_img.convert('RGB')
                    img_buf = BytesIO()
                    bg_img.save(img_buf, format='PNG')
                    img_buf.seek(0)
                    pad = 5
                    c.drawImage(
                        ImageReader(img_buf),
                        fx + pad, sig_y + pad,
                        width=fw - pad * 2, height=sig_h - pad * 2,
                        preserveAspectRatio=True, anchor='c', mask='auto',
                    )
                except Exception:
                    pass

            # ── Typed signature — render as PIL image for cursive font ────────
            elif is_sig:
                # Strip font-prefix if present: "font:Dancing Script|John Doe"
                display_text = value
                font_hint    = 'Dancing Script'
                if value.startswith('font:') and '|' in value:
                    parts        = value.split('|', 1)
                    font_hint    = parts[0][5:]   # strip "font:"
                    display_text = parts[1]

                img_buf = None
                try:
                    img_buf = _typed_sig_to_image(display_text, fw, sig_h, font_name=font_hint)
                except Exception:
                    pass

                if img_buf:
                    pad = 4
                    c.drawImage(
                        ImageReader(img_buf),
                        fx + pad, sig_y + pad,
                        width=fw - pad * 2, height=sig_h - pad * 2,
                        preserveAspectRatio=True, anchor='c', mask='auto',
                    )
                else:
                    # Final fallback: reportlab italic
                    font_size = max(8.0, min(sig_h * 0.62, 30.0))
                    c.setFont('Helvetica-BoldOblique', font_size)
                    c.setFillColorRGB(*DS_INK)
                    c.saveState()
                    p = c.beginPath()
                    p.rect(fx + 4, sig_y, fw - 8, sig_h)
                    c.clipPath(p, stroke=0, fill=0)
                    c.drawString(fx + 6, sig_y + (sig_h - font_size) * 0.40, display_text)
                    c.restoreState()

            # ── Date / text ──────────────────────────────────────────────────
            else:
                font_size = max(7.0, min(sig_h * 0.52, 11.0))
                c.setFont('Helvetica', font_size)
                c.setFillColorRGB(0.08, 0.08, 0.08)
                c.saveState()
                p = c.beginPath()
                p.rect(fx + 3, sig_y, fw - 6, sig_h)
                c.clipPath(p, stroke=0, fill=0)
                c.drawString(fx + 5, sig_y + (sig_h - font_size) * 0.40, value)
                c.restoreState()

            # ── Label strip ──────────────────────────────────────────────────
            if label_h:
                lbl_font = max(5.5, min(label_h * 0.50, 7.5))
                mid_y    = fy + (label_h - lbl_font) * 0.45

                c.setFont('Helvetica-Bold', lbl_font)
                c.setFillColorRGB(*DS_CHECK)
                c.drawString(fx + 3, mid_y, '\u2713')

                c.setFont('Helvetica', lbl_font)
                c.setFillColorRGB(*DS_LABEL)
                if is_sig and field.signer:
                    left_label = f' {field.signer.name[:24]}'
                elif is_sig:
                    left_label = ' Signed'
                elif is_date:
                    left_label = ' Date'
                else:
                    left_label = ' Text'

                c.saveState()
                p = c.beginPath()
                p.rect(fx + 3, fy, fw * 0.62, label_h)
                c.clipPath(p, stroke=0, fill=0)
                c.drawString(fx + 3 + lbl_font, mid_y, left_label)
                c.restoreState()

                if field.filled_at:
                    c.setFont('Helvetica', lbl_font)
                    c.setFillColorRGB(*DS_LABEL)
                    c.drawRightString(fx + fw - 4, mid_y, field.filled_at.strftime('%d %b %Y'))

        c.save()
        overlay_buf.seek(0)
        page.merge_page(PdfReader(overlay_buf).pages[0])
        writer.add_page(page)

    # ── Write signed PDF ──────────────────────────────────────────────────────
    out_buf      = BytesIO()
    writer.write(out_buf)
    signed_bytes = out_buf.getvalue()

    orig_name   = document.name
    signed_name = (orig_name[:-4] + '-signed.pdf') if orig_name.lower().endswith('.pdf') else (orig_name + '-signed.pdf')

    signed_file = SharedFile.objects.create(
        name        = signed_name,
        uploaded_by = document.uploaded_by,
        folder      = document.folder,
        visibility  = document.visibility,
        description = (
            f'Signed copy of "{orig_name}" — '
            f'{sig_req.signers.filter(status="signed").count()} signature(s) collected.'
        ),
        tags      = document.tags,
        file_size = len(signed_bytes),
        file_type = 'application/pdf',
    )
    signed_file.file.save(signed_name, ContentFile(signed_bytes), save=True)

    try:
        signed_file.file_hash = signed_file.compute_hash()
        signed_file.save(update_fields=['file_hash'])
    except Exception:
        pass

    sig_req.document = signed_file
    sig_req.save(update_fields=['document'])

    return signed_file


# ─────────────────────────────────────────────────────────────────────────────
# File Manager
# ─────────────────────────────────────────────────────────────────────────────

class FileManagerView(LoginRequiredMixin, TemplateView):
    template_name = 'files/file_manager.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        req  = self.request

        files   = _visible_files_qs(user).select_related('uploaded_by', 'folder')
        folders = _visible_folders_qs(user).select_related('owner', 'parent')

        q           = req.GET.get('q', '').strip()
        filter_mode = req.GET.get('filter', '')
        type_cat    = req.GET.get('type', '')
        folder_id   = req.GET.get('folder', '')
        sort        = req.GET.get('sort', '-created_at')

        current_folder = None
        if folder_id:
            try:
                current_folder = folders.get(id=folder_id)
                files   = files.filter(folder=current_folder)
                folders = folders.filter(parent=current_folder)
            except FileFolder.DoesNotExist:
                pass
        elif not q and not filter_mode and not type_cat:
            folders = folders.filter(parent__isnull=True)

        if q:
            files = files.filter(
                Q(name__icontains=q)|Q(description__icontains=q)|Q(tags__icontains=q)
            )
        if filter_mode == 'mine':
            files   = files.filter(uploaded_by=user)
            folders = folders.filter(owner=user)
        elif filter_mode == 'shared':
            files = files.filter(shared_with=user)
        elif filter_mode == 'office':
            files = files.filter(visibility='office')
        elif filter_mode == 'recent':
            from datetime import timedelta
            files = files.filter(created_at__gte=timezone.now()-timedelta(days=30))
        elif filter_mode == 'signatures':
            # Show files that have pending signature requests
            pending_file_ids = SignatureRequest.objects.filter(
                Q(created_by=user) | Q(signers__user=user)
            ).values_list('document_id', flat=True).distinct()
            files = files.filter(id__in=pending_file_ids)

        if type_cat:
            from apps.files.models import _TYPE_CATEGORY
            exts = _TYPE_CATEGORY.get(type_cat, set())
            if exts:
                ext_q = Q()
                for ext in exts:
                    ext_q |= Q(name__iendswith=f'.{ext}')
                files = files.filter(ext_q)

        allowed_sorts = {
            'name':'-name','created_at':'created_at','-created_at':'-created_at',
            'file_size':'file_size','-file_size':'-file_size',
            'download_count':'download_count','-download_count':'-download_count',
            '-name':'-name',
        }
        files = files.order_by(allowed_sorts.get(sort, '-created_at'))

        my_qs          = _visible_files_qs(user).filter(uploaded_by=user)
        my_total_size  = my_qs.aggregate(s=Sum('file_size'))['s'] or 0

        # Pending signatures count for badge
        pending_sigs   = SignatureRequestSigner.objects.filter(
            user=user, status='pending'
        ).count()

        from apps.core.models import User as CoreUser
        from apps.organization.models import Unit, Department
        ctx.update({
            'files': files, 'folders': folders,
            'current_folder': current_folder,
            'folder_ancestors': current_folder.ancestors() if current_folder else [],
            'all_folders': _visible_folders_qs(user).order_by('name'),
            'all_staff': CoreUser.objects.filter(is_active=True, status='active'
                ).exclude(id=user.id).order_by('first_name'),
            'all_units': Unit.objects.filter(is_active=True).order_by('name'),
            'all_departments': Department.objects.filter(is_active=True).order_by('name'),
            'visibility_choices': SharedFile.Visibility.choices,
            'type_categories': [
                ('document','Documents','bi-file-earmark-text'),
                ('spreadsheet','Spreadsheets','bi-file-earmark-excel'),
                ('presentation','Presentations','bi-file-earmark-slides'),
                ('image','Images','bi-file-earmark-image'),
                ('video','Videos','bi-file-earmark-play'),
                ('audio','Audio','bi-file-earmark-music'),
                ('archive','Archives','bi-file-earmark-zip'),
                ('code','Code','bi-file-earmark-code'),
            ],
            'q': q, 'filter_mode': filter_mode, 'type_cat': type_cat,
            'folder_id': folder_id, 'sort': sort,
            'my_file_count':    my_qs.count(),
            'my_total_size':    my_total_size,
            'office_count':     _visible_files_qs(user).filter(visibility='office').count(),
            'shared_count':     _visible_files_qs(user).filter(shared_with=user).count(),
            'total_file_count': _visible_files_qs(user).count(),
            'pending_sigs':     pending_sigs,
            # My signature requests
            'my_sig_requests':  SignatureRequest.objects.filter(
                created_by=user
            ).prefetch_related('signers').order_by('-created_at')[:10],
        })
        return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Upload / Download / Delete / Share / Folder actions  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

class FileUploadView(LoginRequiredMixin, View):
    def post(self, request):
        f = request.FILES.get('file')
        if not f:
            messages.error(request, 'No file selected.')
            return redirect('file_manager')
        folder_id = request.POST.get('folder_id')
        folder = None
        if folder_id:
            try: folder = FileFolder.objects.get(id=folder_id)
            except FileFolder.DoesNotExist: pass
        sf = SharedFile.objects.create(
            name=request.POST.get('name', f.name).strip() or f.name,
            file=f, folder=folder, uploaded_by=request.user,
            visibility=request.POST.get('visibility', 'private'),
            description=request.POST.get('description', ''),
            tags=request.POST.get('tags', ''),
            file_size=f.size, file_type=f.content_type,
        )
        # Compute hash in background (don't fail on error)
        try:
            sf.file_hash = sf.compute_hash()
            sf.save(update_fields=['file_hash'])
        except Exception:
            pass
        messages.success(request, f'"{f.name}" uploaded.')
        next_url = request.POST.get('next', '')
        if next_url and next_url.startswith('/'):
            return redirect(next_url)
        if folder_id:
            return redirect(f'/files/?folder={folder_id}')
        return redirect('file_manager')


class FileDownloadView(LoginRequiredMixin, View):
    def get(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)
        user = request.user
        profile = getattr(user, 'staffprofile', None)
        unit, dept = (profile.unit if profile else None), (profile.department if profile else None)
        if not (f.uploaded_by==user or f.visibility=='office' or
                (f.visibility=='unit' and f.unit==unit) or
                (f.visibility=='department' and f.department==dept) or
                f.shared_with.filter(id=user.id).exists()):
            raise Http404
        f.download_count += 1
        f.save(update_fields=['download_count'])
        return FileResponse(f.file.open('rb'), as_attachment=True, filename=f.name)


@method_decorator(xframe_options_sameorigin, name='dispatch')
class FilePreviewView(LoginRequiredMixin, View):
    """
    Serve a file inline for the preview modal.
    For Office documents (docx, xlsx, pptx, etc.) LibreOffice converts them
    to PDF on first request; the result is cached in /tmp so repeat previews
    are instant.  All other types are served as-is.
    """

    _OFFICE_EXTS = {
        'doc', 'docx', 'odt', 'rtf',
        'xls', 'xlsx', 'ods',
        'ppt', 'pptx', 'odp',
    }

    def get(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)
        user = request.user
        profile = getattr(user, 'staffprofile', None)
        unit = profile.unit if profile else None
        dept = profile.department if profile else None

        if not (
            f.uploaded_by == user or
            f.visibility == 'office' or
            (f.visibility == 'unit' and f.unit == unit) or
            (f.visibility == 'department' and f.department == dept) or
            f.shared_with.filter(id=user.id).exists()
        ):
            raise Http404

        ext = f.extension.lower()

        # ── Office documents → convert to PDF, serve inline ──────────────────
        if ext in self._OFFICE_EXTS:
            return self._office_preview(f, ext)

        # ── Text files → plain text so JS can fetch & display ────────────────
        if ext in ('md', 'py', 'js', 'html', 'css', 'sql', 'xml', 'json', 'csv', 'txt'):
            response = FileResponse(f.file.open('rb'), content_type='text/plain; charset=utf-8')
            response['Content-Disposition'] = f'inline; filename="{f.name}"'
            response['X-Frame-Options'] = 'SAMEORIGIN'
            return response

        # ── Everything else (PDF, image, video, audio) ────────────────────────
        content_type, _ = mimetypes.guess_type(f.name)
        content_type = content_type or 'application/octet-stream'
        response = FileResponse(f.file.open('rb'), content_type=content_type)
        response['Content-Disposition'] = f'inline; filename="{f.name}"'
        response['X-Frame-Options'] = 'SAMEORIGIN'
        return response

    def _office_preview(self, f, ext):
        """Convert Office file to PDF (LibreOffice) with a file-system cache."""
        import shutil
        from pathlib import Path

        # Cache dir and key — keyed on pk + file_hash so a new upload busts cache
        cache_dir = Path(tempfile.gettempdir()) / 'eo_preview_cache'
        cache_dir.mkdir(exist_ok=True)
        cache_key  = f'{f.pk}_{f.file_hash or "v1"}.pdf'
        cache_path = cache_dir / cache_key

        if not cache_path.exists():
            # Write original file to a temp path with the correct extension so
            # LibreOffice picks up the right filter automatically.
            src_tmp = tempfile.NamedTemporaryFile(
                suffix=f'.{ext}', delete=False,
                dir=tempfile.gettempdir()
            )
            try:
                src_tmp.write(f.file.open('rb').read())
                src_tmp.flush()
                src_tmp.close()

                pdf_path = _convert_to_pdf(src_tmp.name)   # existing helper
                shutil.copy2(pdf_path, str(cache_path))
            except Exception as exc:
                # Conversion failed — tell the browser so the frontend can show a message
                return HttpResponse(
                    f'Office preview unavailable: {exc}',
                    content_type='text/plain',
                    status=422,
                )
            finally:
                try:
                    os.unlink(src_tmp.name)
                except OSError:
                    pass

        response = FileResponse(
            open(str(cache_path), 'rb'),
            content_type='application/pdf',
        )
        response['Content-Disposition'] = f'inline; filename="{f.name}.preview.pdf"'
        response['X-Frame-Options'] = 'SAMEORIGIN'
        return response


class FileDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)

        if not _can_delete_file(request.user, f):
            messages.error(request, 'You do not have permission to delete this file.')
            return redirect('file_manager')

        name, folder_id = f.name, f.folder_id

        trash = FileTrash.objects.create(
            item_type='file',
            original_file=f,
            name=f.name,
            deleted_by=request.user,
            owner=f.uploaded_by,
            original_parent_folder=f.folder,
            original_visibility=f.visibility,
            original_description=f.description,
            original_tags=f.tags,
        )

        # keep a copy in trash storage
        if f.file:
            f.file.open('rb')
            trash.file_blob.save(os.path.basename(f.file.name), ContentFile(f.file.read()), save=True)

        _log_file_history(f, 'deleted', actor=request.user, notes='Moved to recycle bin')
        f.delete()

        messages.success(request, f'"{name}" moved to recycle bin.')
        return redirect(f'/files/?folder={folder_id}' if folder_id else 'file_manager')

class FileShareView(LoginRequiredMixin, View):
    def get(self, request, pk):
        return redirect('file_manager')

    def post(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk, uploaded_by=request.user)

        vis = (request.POST.get('visibility') or f.visibility or 'private').strip()
        allowed = {'private', 'shared_with', 'unit', 'department', 'office'}
        if vis not in allowed:
            vis = 'private'

        # Track previous direct shares so we only notify newly added users
        previous_shared_ids = set(f.shared_with.values_list('id', flat=True))

        # Reset old sharing state
        f.visibility = vis
        f.unit = None
        f.department = None
        f.save(update_fields=['visibility', 'unit', 'department'])

        # Clear previous direct shares + permission rows
        f.shared_with.clear()
        try:
            f.share_access.all().delete()
        except Exception:
            pass

        added_recipients = []

        if vis == 'shared_with':
            from apps.core.models import User

            for uid in request.POST.getlist('shared_with'):
                try:
                    recipient = User.objects.get(id=uid, is_active=True)
                    if recipient == request.user:
                        continue

                    # Permission per selected user
                    perm = (request.POST.get(f'perm_{uid}', 'view') or 'view').strip().lower()
                    if perm not in ('view', 'edit', 'full'):
                        perm = 'view'

                    # Keep legacy M2M for visibility checks if your app still uses it
                    f.shared_with.add(recipient)

                    # New permission row
                    try:
                        FileShareAccess.objects.update_or_create(
                            file=f,
                            user=recipient,
                            defaults={
                                'permission': perm,
                                'granted_by': request.user,
                            }
                        )
                    except Exception:
                        # If FileShareAccess is not yet added/migrated, sharing still works
                        pass

                    if recipient.id not in previous_shared_ids:
                        added_recipients.append((recipient, perm))

                except Exception:
                    pass

            # Do not keep a broken shared_with state
            if not f.shared_with.exists():
                f.visibility = 'private'
                f.save(update_fields=['visibility'])

        elif vis == 'unit':
            uid2 = (request.POST.get('unit_id') or '').strip()
            if uid2:
                try:
                    from apps.organization.models import Unit
                    f.unit = Unit.objects.get(id=uid2)
                    f.save(update_fields=['unit'])
                except Exception:
                    f.visibility = 'private'
                    f.save(update_fields=['visibility'])
            else:
                f.visibility = 'private'
                f.save(update_fields=['visibility'])

        elif vis == 'department':
            did = (request.POST.get('dept_id') or '').strip()
            if did:
                try:
                    from apps.organization.models import Department
                    f.department = Department.objects.get(id=did)
                    f.save(update_fields=['department'])
                except Exception:
                    f.visibility = 'private'
                    f.save(update_fields=['visibility'])
            else:
                f.visibility = 'private'
                f.save(update_fields=['visibility'])

        # Optional history log
        try:
            _log_file_history(
                f,
                'permission_changed',
                actor=request.user,
                notes=f'Sharing updated: visibility={f.visibility}'
            )
        except Exception:
            pass

        if f.visibility == 'private':
            messages.success(request, f'Sharing stopped for "{f.name}".')
        else:
            messages.success(request, f'Sharing updated for "{f.name}".')

        # Notify newly added direct-share users
        if f.visibility == 'shared_with':
            for recipient, perm in added_recipients:
                _notify(
                    recipient,
                    'file_shared',
                    title=f'{request.user.full_name} shared a file with you',
                    body=f'"{f.name}" was shared with you with {perm} permission.',
                    link=f'/files/{f.pk}/preview/',
                    sender=request.user,
                )
                try:
                    send_mail(
                        subject=f'[EasyOffice] {request.user.full_name} shared "{f.name}" with you',
                        message=(
                            f'Hello {recipient.full_name},\n\n'
                            f'{request.user.full_name} has shared the file "{f.name}" with you.\n'
                            f'Permission: {perm}\n\n'
                            f'View it in EasyOffice Files:\n/files/{f.pk}/preview/\n\n'
                            f'— EasyOffice'
                        ),
                        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@easyoffice.local'),
                        recipient_list=[recipient.email],
                        fail_silently=True,
                    )
                except Exception:
                    pass

        next_url = request.POST.get('next', '')
        return redirect(next_url if next_url.startswith('/') else 'file_manager')


class FolderCreateView(LoginRequiredMixin, View):
    def post(self, request):
        name = request.POST.get('name', '').strip()
        if not name:
            messages.error(request, 'Folder name required.')
            return redirect('file_manager')
        parent_id = request.POST.get('parent_id')
        parent = None
        if parent_id:
            try: parent = FileFolder.objects.get(id=parent_id, owner=request.user)
            except FileFolder.DoesNotExist: pass
        FileFolder.objects.create(
            name=name, owner=request.user, parent=parent,
            visibility=request.POST.get('visibility', 'private'),
            color=request.POST.get('color', '#f59e0b'),
        )
        messages.success(request, f'Folder "{name}" created.')
        next_url = request.POST.get('next', '')
        return redirect(next_url if next_url.startswith('/') else 'file_manager')


class FolderDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        folder = get_object_or_404(FileFolder, pk=pk)

        if not _can_delete_folder(request.user, folder):
            messages.error(request, 'You do not have permission to delete this folder.')
            return redirect('file_manager')

        name, parent_id = folder.name, folder.parent_id

        FileTrash.objects.create(
            item_type='folder',
            original_folder=folder,
            name=folder.name,
            deleted_by=request.user,
            owner=folder.owner,
            original_parent_folder=folder.parent,
            original_visibility=folder.visibility,
        )

        folder.delete()

        messages.success(request, f'Folder "{name}" moved to recycle bin.')
        return redirect(f'/files/?folder={parent_id}' if parent_id else 'file_manager')

class FolderShareView(LoginRequiredMixin, View):
    def post(self, request, pk):
        folder = get_object_or_404(FileFolder, pk=pk, owner=request.user)

        previous_visibility = folder.visibility
        previous_unit_id = folder.unit_id
        previous_department_id = folder.department_id

        # If your FolderShareAccess model exists, track previous direct shares
        try:
            previous_shared_ids = set(folder.share_access.values_list('user_id', flat=True))
        except Exception:
            previous_shared_ids = set()

        vis = (request.POST.get('visibility') or folder.visibility or 'private').strip()
        allowed = {'private', 'shared_with', 'unit', 'department', 'office'}
        if vis not in allowed:
            vis = 'private'

        folder.visibility = vis

        # Reset stale values first
        folder.unit = None
        folder.department = None

        # Clear old direct-share permissions
        try:
            folder.share_access.all().delete()
        except Exception:
            pass

        added_recipients = []

        if folder.visibility == 'shared_with':
            from apps.core.models import User

            for uid in request.POST.getlist('shared_with'):
                try:
                    recipient = User.objects.get(id=uid, is_active=True)
                    if recipient == request.user:
                        continue

                    perm = (request.POST.get(f'perm_{uid}', 'view') or 'view').strip().lower()
                    if perm not in ('view', 'edit', 'full'):
                        perm = 'view'

                    try:
                        FolderShareAccess.objects.update_or_create(
                            folder=folder,
                            user=recipient,
                            defaults={
                                'permission': perm,
                                'granted_by': request.user,
                            }
                        )
                    except Exception:
                        # If FolderShareAccess not migrated yet, ignore silently
                        pass

                    if recipient.id not in previous_shared_ids:
                        added_recipients.append((recipient, perm))

                except Exception:
                    pass

            # If nobody selected, do not leave broken shared state
            try:
                if not folder.share_access.exists():
                    folder.visibility = 'private'
            except Exception:
                folder.visibility = 'private'

        elif folder.visibility == 'unit':
            uid = (request.POST.get('unit_id') or '').strip()
            if uid:
                try:
                    from apps.organization.models import Unit
                    folder.unit = Unit.objects.get(id=uid)
                except Exception:
                    folder.visibility = 'private'
            else:
                folder.visibility = 'private'

        elif folder.visibility == 'department':
            did = (request.POST.get('dept_id') or '').strip()
            if did:
                try:
                    from apps.organization.models import Department
                    folder.department = Department.objects.get(id=did)
                except Exception:
                    folder.visibility = 'private'
            else:
                folder.visibility = 'private'

        folder.save()

        # Optional history log
        try:
            _log_folder_history(
                folder,
                'permission_changed',
                actor=request.user,
                notes=f'Sharing updated: visibility={folder.visibility}'
            )
        except Exception:
            pass

        if folder.visibility == 'private':
            messages.success(request, f'Sharing stopped for "{folder.name}".')
        else:
            messages.success(request, f'Sharing updated for "{folder.name}".')

        # Notify users when folder scope changes or new direct shares are added
        try:
            from apps.core.models import User

            # Direct per-user folder shares
            if folder.visibility == 'shared_with':
                for recipient, perm in added_recipients:
                    _notify(
                        recipient,
                        'file_shared',
                        title=f'{request.user.full_name} shared a folder with you',
                        body=f'Folder "{folder.name}" was shared with you with {perm} permission.',
                        link='/files/',
                        sender=request.user,
                    )
                    try:
                        send_mail(
                            subject=f'[EasyOffice] {request.user.full_name} shared folder "{folder.name}" with you',
                            message=(
                                f'Hello {recipient.full_name},\n\n'
                                f'{request.user.full_name} has shared the folder "{folder.name}" with you.\n'
                                f'Permission: {perm}\n\n'
                                f'Open EasyOffice Files to access it.\n\n'
                                f'— EasyOffice'
                            ),
                            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@easyoffice.local'),
                            recipient_list=[recipient.email],
                            fail_silently=True,
                        )
                    except Exception:
                        pass

            # Scope-based sharing notifications
            else:
                recipients = User.objects.none()

                changed = (
                    previous_visibility != folder.visibility or
                    previous_unit_id != folder.unit_id or
                    previous_department_id != folder.department_id
                )

                if changed:
                    if folder.visibility == 'unit' and folder.unit_id:
                        recipients = User.objects.filter(
                            is_active=True,
                            staffprofile__unit_id=folder.unit_id
                        ).exclude(id=request.user.id).distinct()

                    elif folder.visibility == 'department' and folder.department_id:
                        recipients = User.objects.filter(
                            is_active=True,
                            staffprofile__department_id=folder.department_id
                        ).exclude(id=request.user.id).distinct()

                    elif folder.visibility == 'office':
                        recipients = User.objects.filter(
                            is_active=True
                        ).exclude(id=request.user.id).distinct()

                    for recipient in recipients:
                        _notify(
                            recipient,
                            'file_shared',
                            title=f'{request.user.full_name} shared a folder with you',
                            body=f'Folder "{folder.name}" is now available to you.',
                            link='/files/',
                            sender=request.user,
                        )
                        try:
                            send_mail(
                                subject=f'[EasyOffice] {request.user.full_name} shared folder "{folder.name}" with you',
                                message=(
                                    f'Hello {recipient.full_name},\n\n'
                                    f'{request.user.full_name} has shared the folder "{folder.name}" with you.\n\n'
                                    f'Open EasyOffice Files to access it.\n\n'
                                    f'— EasyOffice'
                                ),
                                from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@easyoffice.local'),
                                recipient_list=[recipient.email],
                                fail_silently=True,
                            )
                        except Exception:
                            pass
        except Exception:
            pass

        next_url = request.POST.get('next', '')
        return redirect(next_url if next_url.startswith('/') else 'file_manager')

class RecycleBinView(LoginRequiredMixin, TemplateView):
    template_name = 'files/recycle_bin.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['trash_items'] = FileTrash.objects.filter(owner=self.request.user, is_restored=False)
        return ctx


class RestoreTrashItemView(LoginRequiredMixin, View):
    def post(self, request, pk):
        item = get_object_or_404(FileTrash, pk=pk, owner=request.user, is_restored=False)

        if item.item_type == 'file':
            restored = SharedFile.objects.create(
                name=item.name,
                uploaded_by=item.owner,
                folder=item.original_parent_folder,
                visibility=item.original_visibility or 'private',
                description=item.original_description or '',
                tags=item.original_tags or '',
                file_size=item.file_blob.size if item.file_blob else 0,
                file_type='application/octet-stream',
            )
            if item.file_blob:
                item.file_blob.open('rb')
                restored.file.save(os.path.basename(item.file_blob.name), ContentFile(item.file_blob.read()), save=True)
            _log_file_history(restored, 'restored', actor=request.user, notes='Restored from recycle bin')

        item.is_restored = True
        item.restored_at = timezone.now()
        item.save(update_fields=['is_restored', 'restored_at'])

        messages.success(request, f'"{item.name}" restored.')
        return redirect('recycle_bin')


class FileHistoryView(LoginRequiredMixin, TemplateView):
    template_name = 'files/file_history.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        f = get_object_or_404(SharedFile, pk=self.kwargs['pk'])
        if not _file_permission_for(self.request.user, f):
            raise Http404
        ctx['file_obj'] = f
        ctx['history_rows'] = f.history_rows.select_related('actor')
        return ctx

# ─────────────────────────────────────────────────────────────────────────────
# PDF Conversion
# ─────────────────────────────────────────────────────────────────────────────

class ConvertToPDFView(LoginRequiredMixin, View):
    def post(self, request, pk):
        sf = get_object_or_404(SharedFile, pk=pk, uploaded_by=request.user)

        if not sf.is_convertible:
            messages.error(request, f'"{sf.name}" cannot be converted to PDF.')
            return redirect('file_manager')

        if sf.is_pdf:
            messages.info(request, 'This file is already a PDF.')
            return redirect('file_manager')

        tmp_path = None
        pdf_path = None

        try:
            suffix = '.' + sf.extension
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = tmp.name
                for chunk in sf.file.chunks():
                    tmp.write(chunk)

            pdf_name = os.path.splitext(sf.name)[0] + '.pdf'

            image_exts = {'jpg', 'jpeg', 'png', 'webp', 'bmp', 'tiff'}

            if sf.extension.lower() in image_exts:
                pdf_buffer = _convert_image_to_pdf(tmp_path)
                file_size = pdf_buffer.getbuffer().nbytes

                new_sf = SharedFile.objects.create(
                    name=pdf_name,
                    uploaded_by=request.user,
                    folder=sf.folder,
                    visibility=sf.visibility,
                    description=f'Converted from image {sf.name}',
                    tags=sf.tags,
                    file_size=file_size,
                    file_type='application/pdf',
                )
                new_sf.file.save(pdf_name, ContentFile(pdf_buffer.read()), save=True)

            else:
                pdf_path = _convert_to_pdf(tmp_path)

                with open(pdf_path, 'rb') as pdf_f:
                    from django.core.files import File as DjangoFile
                    new_sf = SharedFile.objects.create(
                        name=pdf_name,
                        file=DjangoFile(pdf_f, name=pdf_name),
                        folder=sf.folder,
                        uploaded_by=request.user,
                        visibility=sf.visibility,
                        description=f'Converted from {sf.name}',
                        tags=sf.tags,
                        file_size=os.path.getsize(pdf_path),
                        file_type='application/pdf',
                    )

            try:
                new_sf.file_hash = new_sf.compute_hash()
                new_sf.save(update_fields=['file_hash'])
            except Exception:
                pass

            for sig_req in sf.signature_requests.filter(status__in=['draft', 'sent', 'partial']):
                _log_audit(
                    sig_req,
                    'converted',
                    request=request,
                    notes=f'Converted {sf.name} → {pdf_name}'
                )

            messages.success(request, f'✓ "{sf.name}" converted to PDF — "{pdf_name}" added to your files.')

        except RuntimeError as e:
            messages.error(request, str(e))
        except Exception as e:
            messages.error(request, f'Conversion failed: {e}')
        finally:
            try:
                if tmp_path:
                    os.unlink(tmp_path)
            except Exception:
                pass
            try:
                if pdf_path:
                    os.unlink(pdf_path)
            except Exception:
                pass

        next_url = request.POST.get('next', '')
        return redirect(next_url if next_url.startswith('/') else 'file_manager')

# ─────────────────────────────────────────────────────────────────────────────
# Signature Flow — Create / Manage
# ─────────────────────────────────────────────────────────────────────────────

class SignatureRequestCreateView(LoginRequiredMixin, View):
    template_name = 'files/signature_request.html'

    def get(self, request, pk=None):
        document = None
        if pk:
            document = get_object_or_404(SharedFile, pk=pk, uploaded_by=request.user)
        ctx = {
            'document': document,
            'my_files': _visible_files_qs(request.user).filter(
                uploaded_by=request.user
            ).order_by('-created_at')[:50],
            'all_staff': __import__('apps.core.models', fromlist=['User']).User.objects.filter(
                is_active=True, status='active'
            ).exclude(id=request.user.id).order_by('first_name'),
        }
        return render(request, self.template_name, ctx)

    def post(self, request, pk=None):
        doc_id = request.POST.get('document_id') or (str(pk) if pk else None)
        document = get_object_or_404(SharedFile, pk=doc_id)

        # ── AUTO-CONVERT TO PDF ──────────────────────────────────────────────
        # Signature requests always use a PDF so signers can view in-browser.
        if not document.is_pdf and document.is_convertible:
            tmp_path = None
            pdf_path = None
            try:
                suffix = '.' + document.extension
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp_path = tmp.name
                    for chunk in document.file.chunks():
                        tmp.write(chunk)

                pdf_name = os.path.splitext(document.name)[0] + '.pdf'
                image_exts = {'jpg', 'jpeg', 'png', 'webp', 'bmp', 'tiff'}

                if document.extension.lower() in image_exts:
                    pdf_buffer = _convert_image_to_pdf(tmp_path)

                    document = SharedFile.objects.create(
                        name=pdf_name,
                        uploaded_by=request.user,
                        folder=document.folder,
                        visibility=document.visibility,
                        description=f'Auto-converted for signature from {document.name}',
                        tags=document.tags,
                        file_size=pdf_buffer.getbuffer().nbytes,
                        file_type='application/pdf',
                    )
                    document.file.save(pdf_name, ContentFile(pdf_buffer.read()), save=True)

                else:
                    pdf_path = _convert_to_pdf(tmp_path)

                    with open(pdf_path, 'rb') as pdf_f:
                        from django.core.files import File as DjangoFile
                        document = SharedFile.objects.create(
                            name=pdf_name,
                            file=DjangoFile(pdf_f, name=pdf_name),
                            folder=document.folder,
                            uploaded_by=request.user,
                            visibility=document.visibility,
                            description=f'Auto-converted for signature from {document.name}',
                            tags=document.tags,
                            file_size=os.path.getsize(pdf_path),
                            file_type='application/pdf',
                        )

                messages.info(request, 'Document auto-converted to PDF for signing.')

            except RuntimeError as e:
                messages.warning(request, f'Could not auto-convert to PDF ({e}). Proceeding with original file.')
            except Exception as e:
                messages.warning(request, f'Auto-conversion skipped: {e}')
            finally:
                try:
                    if tmp_path:
                        os.unlink(tmp_path)
                except Exception:
                    pass
                try:
                    if pdf_path:
                        os.unlink(pdf_path)
                except Exception:
                    pass
        # ────────────────────────────────────────────────────────────────────

        # Build signer list from POST
        signer_names  = request.POST.getlist('signer_name')
        signer_emails = request.POST.getlist('signer_email')
        if not signer_emails or not any(e.strip() for e in signer_emails):
            messages.error(request, 'At least one signer is required.')
            return redirect(request.path)

        sig_req = SignatureRequest.objects.create(
            title=request.POST.get('title', document.name),
            message=request.POST.get('message', ''),
            document=document,
            created_by=request.user,
            status=SignatureRequest.Status.SENT,
            ordered_signing='ordered' in request.POST,
        )
        if request.POST.get('expires_days'):
            from datetime import timedelta
            try:
                days = int(request.POST['expires_days'])
                sig_req.expires_at = timezone.now() + timedelta(days=days)
                sig_req.save(update_fields=['expires_at'])
            except ValueError:
                pass

        from apps.core.models import User as CoreUser
        base_url = request.build_absolute_uri('/')

        for i, (name, email) in enumerate(zip(signer_names, signer_emails)):
            name, email = name.strip(), email.strip()
            if not email:
                continue
            user_obj = CoreUser.objects.filter(email=email).first()
            signer = SignatureRequestSigner.objects.create(
                request=sig_req, user=user_obj,
                email=email, name=name or (user_obj.full_name if user_obj else email),
                order=i+1,
            )
            _notify_signer(signer, base_url)   # email (all signers)
            if user_obj:                        # WS push (internal users only)
                _notify(
                    user_obj, 'sign_request',
                    title=f'Please sign: {sig_req.title}',
                    body=f'Requested by {request.user.full_name}',
                    link=signer.signing_url,
                )

        _log_audit(sig_req, 'created', request=request,
                   notes=f'Created by {request.user.full_name}')
        _log_audit(sig_req, 'sent', request=request,
                   notes=f'{sig_req.signers.count()} signer(s) notified')

        # Save field placements from the annotation canvas
        import json as _json
        fields_json = request.POST.get('fields_json', '').strip()
        if fields_json:
            try:
                from apps.files.models import SignatureField
                field_data = _json.loads(fields_json)
                for f in field_data:
                    signer_order = int(f.get('signer_order', 1))
                    signer_obj = sig_req.signers.filter(order=signer_order).first()
                    SignatureField.objects.create(
                        request=sig_req,
                        signer=signer_obj,
                        field_type=f.get('type', 'signature'),
                        page=int(f.get('page', 1)),
                        x_pct=float(f.get('x', 10)),
                        y_pct=float(f.get('y', 10)),
                        width_pct=float(f.get('w', 20)),
                        height_pct=float(f.get('h', 5)),
                        label=f.get('label', ''),
                    )
            except Exception:
                pass

        messages.success(request, f'Signature request "{sig_req.title}" sent to {sig_req.signers.count()} signer(s).')
        return redirect('signature_request_detail', pk=sig_req.pk)


class SignatureRequestDetailView(LoginRequiredMixin, View):
    template_name = 'files/signature_detail.html'

    def get(self, request, pk):
        sig_req = get_object_or_404(
            SignatureRequest.objects.distinct(),
            Q(created_by=request.user) | Q(signers__user=request.user),
            pk=pk,
        )
        return render(request, self.template_name, {
            'sig_req':    sig_req,
            'signers':    sig_req.signers.all(),
            'audit':      sig_req.audit_trail.all(),
            'is_creator': sig_req.created_by == request.user,
        })

    def post(self, request, pk):
        """Cancel or resend."""
        sig_req = get_object_or_404(SignatureRequest, pk=pk, created_by=request.user)
        action = request.POST.get('action')
        if action == 'cancel':
            sig_req.status = SignatureRequest.Status.CANCELLED
            sig_req.save(update_fields=['status'])
            _log_audit(sig_req, 'cancelled', request=request)
            messages.warning(request, 'Signature request cancelled.')
        elif action == 'resend':
            signer_id = request.POST.get('signer_id')
            signer = get_object_or_404(SignatureRequestSigner, pk=signer_id, request=sig_req)
            base_url = request.build_absolute_uri('/')
            _notify_signer(signer, base_url)   # email
            if signer.user:                    # WS push
                _notify(
                    signer.user, 'sign_reminder',
                    title=f'Reminder: Please sign "{sig_req.title}"',
                    body=f'Requested by {sig_req.created_by.full_name}',
                    link=signer.signing_url,
                )
            messages.success(request, f'Reminder sent to {signer.email}.')
        return redirect('signature_request_detail', pk=sig_req.pk)


class CreatorSignView(LoginRequiredMixin, View):
    """
    Lets the creator sign their own document when they are a required
    signer in the flow (i.e. a SignatureRequestSigner row exists for them).
    Redirects straight to the token-based signing page using their signer token.
    If they are not a signer, shows an error.
    """
    def get(self, request, pk):
        sig_req = get_object_or_404(
            SignatureRequest,
            pk=pk,
            created_by=request.user,
        )
        # Find the creator's own signer row
        signer = sig_req.signers.filter(user=request.user).first()
        if not signer:
            messages.error(
                request,
                'You are not listed as a required signer on this document. '
                'To sign it yourself, add your own name and email when creating the request.'
            )
            return redirect('signature_request_detail', pk=sig_req.pk)

        if signer.status in ('signed', 'declined'):
            messages.info(request, 'You have already responded to this document.')
            return redirect('signature_request_detail', pk=sig_req.pk)

        # Redirect to the standard token-based signing page
        return redirect('sign_document', token=signer.token)


# ─────────────────────────────────────────────────────────────────────────────
# Signing Interface — public (token-based, no login required)
# ─────────────────────────────────────────────────────────────────────────────

class SignDocumentView(View):
    """
    Token-based signing page — no login required.
    The unique token in the URL identifies the signer.
    """
    template_name = 'files/sign_document.html'

    def _get_signer(self, token):
        return get_object_or_404(SignatureRequestSigner, token=token)

    def get(self, request, token):
        signer = self._get_signer(token)
        sig_req = signer.request

        if sig_req.status in ('cancelled', 'expired'):
            return render(request, self.template_name, {
                'error': 'This signing request is no longer available.',
                'signer': signer,
                'sig_req': sig_req,
            })

        if signer.status in ('signed', 'declined'):
            return render(request, self.template_name, {
                'already_done': True,
                'signer': signer,
                'sig_req': sig_req,
                'document': sig_req.document,
                'audit': sig_req.audit_trail.all() if hasattr(sig_req, 'audit_trail') else [],
            })

        # Ordered signing — previous signer must finish first
        if getattr(sig_req, 'ordered_signing', False) and signer.order > 1:
            prev_pending = sig_req.signers.filter(order__lt=signer.order).exclude(status='signed').first()
            if prev_pending:
                return render(request, self.template_name, {
                    'error': 'Please wait — a previous signer has not yet completed their signature.',
                    'signer': signer,
                    'sig_req': sig_req,
                })

        # Mark as viewed
        if signer.status == 'pending':
            signer.status = 'viewed'
            signer.viewed_at = timezone.now()
            signer.ip_address = _get_client_ip(request)
            signer.save(update_fields=['status', 'viewed_at', 'ip_address'])
            _log_audit(sig_req, 'viewed', signer=signer, request=request)
            # Notify creator via WS (no email — too noisy for a view event)
            if sig_req.created_by != signer.user:
                _notify(
                    sig_req.created_by, 'sign_viewed',
                    title=f'{signer.name} viewed your document',
                    body=sig_req.title,
                    link=f'/files/signatures/{sig_req.pk}/',
                )

        return render(request, self.template_name, {
            'signer': signer,
            'sig_req': sig_req,
            'document': sig_req.document,
            'audit': sig_req.audit_trail.all() if hasattr(sig_req, 'audit_trail') else [],
            'my_fields': signer.fields.all() if hasattr(signer, 'fields') else [],
        })

    def post(self, request, token):
        signer = self._get_signer(token)
        sig_req = signer.request
        action = request.POST.get('action')

        if signer.status in ('signed', 'declined'):
            messages.info(request, 'You have already responded.')
            return redirect('sign_document', token=token)

        if action == 'sign':
            sig_data = request.POST.get('signature_data', '').strip()
            sig_type = request.POST.get('signature_type', 'draw')

            if not sig_data:
                messages.error(request, 'Signature is required.')
                return redirect('sign_document', token=token)

            # Require all signer fields to be completed before final sign
            if hasattr(signer, 'fields'):
                my_total_fields = signer.fields.count()
                my_filled_fields = signer.fields.exclude(value='').count()
                if my_total_fields and my_filled_fields != my_total_fields:
                    messages.error(request, 'Please complete all required fields before signing.')
                    return redirect('sign_document', token=token)

            signer.status = 'signed'
            signer.signed_at = timezone.now()
            signer.signature_data = sig_data
            signer.signature_type = sig_type
            signer.ip_address = _get_client_ip(request)
            signer.user_agent = request.META.get('HTTP_USER_AGENT', '')[:500]
            signer.save()

            if signer.user and request.POST.get('save_signature') == '1':
                try:
                    from apps.files.models import SavedSignature
                    sig_name = request.POST.get('save_signature_name', 'My Signature').strip() or 'My Signature'
                    SavedSignature.objects.create(
                        user=signer.user,
                        name=sig_name,
                        sig_type=sig_type,
                        data=sig_data,
                    )
                except Exception:
                    pass

            _log_audit(
                sig_req,
                'signed',
                signer=signer,
                request=request,
                notes=f'Signature type: {sig_type}'
            )

            # ── Notify creator: someone signed ─────────────────────────────
            if sig_req.created_by != signer.user:
                _notify(
                    sig_req.created_by, 'sign_signed',
                    title=f'{signer.name} signed your document',
                    body=sig_req.title,
                    link=f'/files/signatures/{sig_req.pk}/',
                )
                try:
                    send_mail(
                        subject=f'[EasyOffice] {signer.name} signed "{sig_req.title}"',
                        message=(
                            f'Hello {sig_req.created_by.full_name},\n\n'
                            f'{signer.name} has signed your document "{sig_req.title}".\n\n'
                            f'View the audit trail:\n/files/signatures/{sig_req.pk}/\n\n'
                            f'— EasyOffice'
                        ),
                        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@easyoffice.local'),
                        recipient_list=[sig_req.created_by.email],
                        fail_silently=True,
                    )
                except Exception:
                    pass

            if hasattr(sig_req, 'update_status'):
                sig_req.update_status()

            if getattr(sig_req, 'status', None) == 'completed':
                _log_audit(sig_req, 'completed', request=request)

                # ── Burn all signatures into the PDF ──────────────────────
                try:
                    _embed_signatures_in_pdf(sig_req)
                except Exception:
                    pass  # PDF stamping failure must never block a completed signing

                # ── Notify creator: all done (WS + email) ─────────────────
                _notify(
                    sig_req.created_by, 'sign_completed',
                    title=f'All signatures collected: {sig_req.title}',
                    body=f'{sig_req.signers.count()} signer(s) have all signed.',
                    link=f'/files/signatures/{sig_req.pk}/',
                )
                try:
                    send_mail(
                        subject=f'✓ All signatures collected: {sig_req.title}',
                        message=(
                            f'Your document "{sig_req.title}" has been signed by all parties.\n\n'
                            f'You can view the signed PDF and audit trail in EasyOffice Files.'
                        ),
                        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@easyoffice.local'),
                        recipient_list=[sig_req.created_by.email],
                        fail_silently=True,
                    )
                except Exception:
                    pass

            messages.success(request, 'Thank you! Your signature has been recorded.')
            return redirect('sign_document', token=token)

        if action == 'decline':
            signer.status = 'declined'
            signer.decline_reason = request.POST.get('decline_reason', '').strip()
            signer.ip_address = _get_client_ip(request)
            signer.user_agent = request.META.get('HTTP_USER_AGENT', '')[:500]
            signer.save(update_fields=['status', 'decline_reason', 'ip_address', 'user_agent'])

            _log_audit(sig_req, 'declined', signer=signer, request=request, notes=signer.decline_reason)

            # ── Notify creator: someone declined (WS + email) ─────────────
            if sig_req.created_by != signer.user:
                reason_suffix = f' Reason: {signer.decline_reason}' if signer.decline_reason else ''
                _notify(
                    sig_req.created_by, 'sign_declined',
                    title=f'{signer.name} declined to sign',
                    body=f'{sig_req.title}.{reason_suffix}',
                    link=f'/files/signatures/{sig_req.pk}/',
                )
                try:
                    send_mail(
                        subject=f'[EasyOffice] {signer.name} declined to sign "{sig_req.title}"',
                        message=(
                            f'Hello {sig_req.created_by.full_name},\n\n'
                            f'{signer.name} has declined to sign "{sig_req.title}".'
                            + (f'\n\nReason: {signer.decline_reason}' if signer.decline_reason else '')
                            + f'\n\nView the request:\n/files/signatures/{sig_req.pk}/\n\n'
                            f'— EasyOffice'
                        ),
                        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@easyoffice.local'),
                        recipient_list=[sig_req.created_by.email],
                        fail_silently=True,
                    )
                except Exception:
                    pass

            if hasattr(sig_req, 'update_status'):
                sig_req.update_status()

            messages.warning(request, 'You declined to sign this document.')
            return redirect('sign_document', token=token)

        messages.error(request, 'Invalid action.')
        return redirect('sign_document', token=token)


# ─────────────────────────────────────────────────────────────────────────────
# My Signatures — in-app inbox for pending signing requests
# ─────────────────────────────────────────────────────────────────────────────

class MySignaturesView(LoginRequiredMixin, TemplateView):
    """
    Dedicated inbox showing all signature requests the current user
    needs to action (pending) or has already handled.
    """
    template_name = 'files/my_signatures.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user

        my_signer_rows = SignatureRequestSigner.objects.filter(
            user=user
        ).select_related(
            'request', 'request__document', 'request__created_by'
        ).order_by('request__created_at')

        ctx.update({
            'pending':    [s for s in my_signer_rows if s.status == 'pending'],
            'viewed':     [s for s in my_signer_rows if s.status == 'viewed'],
            'signed':     [s for s in my_signer_rows if s.status == 'signed'],
            'declined':   [s for s in my_signer_rows if s.status == 'declined'],
            'sent_by_me': SignatureRequest.objects.filter(
                created_by=user
            ).prefetch_related('signers').order_by('-created_at')[:20],
        })
        return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Convert to PDF — standalone dedicated page
# ─────────────────────────────────────────────────────────────────────────────

class ConvertToPDFPageView(LoginRequiredMixin, TemplateView):
    """
    Dedicated page listing all user's convertible files
    with one-click conversion to PDF.
    """
    template_name = 'files/convert_pdf.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        all_mine = list(_visible_files_qs(user).filter(
            uploaded_by=user
        ).order_by('-created_at'))
        ctx['convertible_files'] = [f for f in all_mine if f.is_convertible]
        ctx['pdf_files']         = [f for f in all_mine if f.is_pdf]
        return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Signature Field API — save field placements (AJAX POST from annotation UI)
# ─────────────────────────────────────────────────────────────────────────────

class SaveSignatureFieldsView(LoginRequiredMixin, View):
    """
    Called by the annotation canvas when the creator saves field placements.
    Expects JSON body: { "fields": [ {page, type, x, y, w, h, label, signer_id}, ... ] }
    """
    def post(self, request, pk):
        import json as _json
        sig_req = get_object_or_404(SignatureRequest, pk=pk, created_by=request.user)

        try:
            body = _json.loads(request.body)
            fields = body.get('fields', [])
        except Exception:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        # Replace all existing fields
        from apps.files.models import SignatureField
        sig_req.fields.all().delete()

        for f in fields:
            signer = None
            signer_id = f.get('signer_id')
            if signer_id:
                try:
                    from apps.files.models import SignatureRequestSigner
                    signer = sig_req.signers.get(id=signer_id)
                except Exception:
                    pass
            SignatureField.objects.create(
                request=sig_req,
                signer=signer,
                field_type=f.get('type', 'signature'),
                page=int(f.get('page', 1)),
                x_pct=float(f.get('x', 10)),
                y_pct=float(f.get('y', 10)),
                width_pct=float(f.get('w', 20)),
                height_pct=float(f.get('h', 5)),
                label=f.get('label', ''),
                required=f.get('required', True),
            )

        return JsonResponse({'status': 'ok', 'count': sig_req.fields.count()})


# ─────────────────────────────────────────────────────────────────────────────
# Fill a single field value (AJAX POST from signing page)
# ─────────────────────────────────────────────────────────────────────────────

class FillSignatureFieldView(View):
    """
    Token-based — no login required.
    Called when a signer fills one specific field from the signing page.
    """
    def post(self, request, token, field_id):
        import json as _json
        from apps.files.models import SignatureField

        signer = get_object_or_404(SignatureRequestSigner, token=token)

        # Optional ordered signing guard
        sig_req = signer.request
        if sig_req.ordered_signing and signer.order > 1:
            prev_pending = sig_req.signers.filter(order__lt=signer.order).exclude(status='signed').exists()
            if prev_pending:
                return JsonResponse({
                    'status': 'error',
                    'message': 'A previous signer must complete their fields first.'
                }, status=403)

        field = get_object_or_404(
            SignatureField,
            id=field_id,
            request=sig_req,
            signer=signer
        )

        try:
            body = _json.loads(request.body or '{}')
        except Exception:
            return JsonResponse({
                'status': 'error',
                'message': 'Invalid JSON payload.'
            }, status=400)

        value = (body.get('value') or '').strip()
        field_type = (body.get('type') or field.field_type).strip()

        if not value:
            return JsonResponse({
                'status': 'error',
                'message': 'No signature value was provided.'
            }, status=400)

        # Save field value
        field.value = value
        field.filled_at = timezone.now()
        field.save(update_fields=['value', 'filled_at'])

        # If signer was still pending, mark viewed at minimum
        changed = []
        if signer.status == SignatureRequestSigner.Status.PENDING:
            signer.status = SignatureRequestSigner.Status.VIEWED
            changed.append('status')

        if not signer.viewed_at:
            signer.viewed_at = timezone.now()
            changed.append('viewed_at')

        if not signer.ip_address:
            signer.ip_address = _get_client_ip(request)
            changed.append('ip_address')

        if changed:
            signer.save(update_fields=changed)

        my_total_fields = signer.fields.count()
        my_filled_fields = signer.fields.exclude(value='').count()
        all_fields_done = (my_total_fields == 0) or (my_total_fields == my_filled_fields)

        return JsonResponse({
            'status': 'ok',
            'field_id': str(field.id),
            'field_type': field.field_type,
            'filled_fields': my_filled_fields,
            'total_fields': my_total_fields,
            'all_fields_done': all_fields_done,
            'message': 'Field signed successfully.'
        })

# ─────────────────────────────────────────────────────────────────────────────
# Saved Signatures — manage user's saved signatures
# ─────────────────────────────────────────────────────────────────────────────

class SavedSignaturesView(LoginRequiredMixin, View):
    template_name = 'files/saved_signatures.html'

    def get(self, request):
        from apps.files.models import SavedSignature
        return render(request, self.template_name, {
            'signatures': SavedSignature.objects.filter(user=request.user),
        })

    def post(self, request):
        from apps.files.models import SavedSignature
        action = request.POST.get('action')

        if action == 'save':
            sig_type = request.POST.get('sig_type', 'draw')
            sig = SavedSignature(
                user=request.user,
                name=request.POST.get('name', 'My Signature').strip() or 'My Signature',
                sig_type=sig_type,
                is_default='is_default' in request.POST,
            )
            if sig_type == 'draw':
                sig.data = request.POST.get('data', '')
            elif sig_type == 'type':
                sig.data = request.POST.get('data', '').strip()
            elif sig_type == 'upload' and 'image' in request.FILES:
                sig.image = request.FILES['image']
            sig.save()
            messages.success(request, f'Signature "{sig.name}" saved.')

        elif action == 'delete':
            sig_id = request.POST.get('sig_id')
            get_object_or_404(SavedSignature, id=sig_id, user=request.user).delete()
            messages.success(request, 'Signature deleted.')

        elif action == 'set_default':
            sig_id = request.POST.get('sig_id')
            sig = get_object_or_404(SavedSignature, id=sig_id, user=request.user)
            sig.is_default = True
            sig.save()
            messages.success(request, f'"{sig.name}" set as default.')

        return redirect('saved_signatures')


class SignDocumentPreviewView(View):
    """
    Token-based PDF preview endpoint for the signing page.
    No login required.
    Returns the document inline so PDF.js can render it.
    """
    def get(self, request, token):
        signer = get_object_or_404(SignatureRequestSigner, token=token)
        sig_req = signer.request
        document = sig_req.document

        if sig_req.status in ('cancelled', 'expired'):
            raise Http404("This signing request is no longer available.")

        if not document or not getattr(document, 'file', None):
            raise Http404("Document file not found.")

        file_name = document.name or os.path.basename(document.file.name)

        response = FileResponse(
            document.file.open('rb'),
            content_type='application/pdf'
        )
        response['Content-Disposition'] = f'inline; filename="{file_name}"'
        response['Accept-Ranges'] = 'bytes'
        return response


class SignDocumentDownloadView(View):
    """
    Token-based document download endpoint for the signer.
    No login required.
    """
    def get(self, request, token):
        signer = get_object_or_404(SignatureRequestSigner, token=token)
        sig_req = signer.request
        document = sig_req.document

        if sig_req.status in ('cancelled', 'expired'):
            raise Http404("This signing request is no longer available.")

        if not document or not getattr(document, 'file', None):
            raise Http404("Document file not found.")

        file_name = document.name or os.path.basename(document.file.name)
        content_type, _ = mimetypes.guess_type(file_name)
        content_type = content_type or 'application/octet-stream'

        response = FileResponse(
            document.file.open('rb'),
            content_type=content_type
        )
        response['Content-Disposition'] = f'attachment; filename="{file_name}"'
        return response

class SavedSignatureAPIView(LoginRequiredMixin, View):
    """Return user's saved signatures as JSON for the signing page."""
    def get(self, request):
        from apps.files.models import SavedSignature
        sigs = []
        for s in SavedSignature.objects.filter(user=request.user):
            entry = {
                'id':         str(s.id),
                'name':       s.name,
                'type':       s.sig_type,
                'is_default': s.is_default,
            }
            if s.sig_type == 'draw':
                entry['data'] = s.data
            elif s.sig_type == 'type':
                # Parse stored "font:Dancing Script|John Doe" format,
                # or fall back gracefully for sigs saved before this format.
                raw = s.data or ''
                if raw.startswith('font:') and '|' in raw:
                    parts        = raw.split('|', 1)
                    entry['font'] = parts[0][5:]   # strip leading "font:"
                    entry['text'] = parts[1]
                else:
                    entry['font'] = 'Dancing Script'
                    entry['text'] = raw
                entry['data'] = raw   # keep raw for backward compat
            elif s.image:
                entry['data'] = request.build_absolute_uri(s.image.url)
            sigs.append(entry)
        return JsonResponse({'signatures': sigs})

class FileMoveView(LoginRequiredMixin, View):
    def post(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk, uploaded_by=request.user)

        folder_id = (request.POST.get('folder_id') or '').strip()
        folder = None

        if folder_id:
            folder = get_object_or_404(FileFolder, id=folder_id, owner=request.user)

        f.folder = folder
        f.save(update_fields=['folder'])

        return JsonResponse({
            'status': 'ok',
            'message': f'"{f.name}" moved successfully.',
            'folder_id': str(folder.id) if folder else '',
            'folder_name': folder.name if folder else 'My Drive',
        })

class PDFMergeView(LoginRequiredMixin, View):
    def post(self, request):
        file_ids = request.POST.getlist('file_ids')
        output_name = request.POST.get('output_name', '').strip() or 'merged.pdf'

        if len(file_ids) < 2:
            messages.error(request, 'Please select at least two PDF files to merge.')
            return redirect('pdf_tools_page')

        pdfs = list(
            SharedFile.objects.filter(
                id__in=file_ids,
                uploaded_by=request.user
            )
        )

        if len(pdfs) < 2:
            messages.error(request, 'Could not find the selected PDF files.')
            return redirect('pdf_tools_page')

        writer = PdfWriter()

        try:
            for pdf in pdfs:
                if not getattr(pdf, 'is_pdf', False):
                    continue
                reader = PdfReader(pdf.file.open('rb'))
                for page in reader.pages:
                    writer.add_page(page)

            buffer = BytesIO()
            writer.write(buffer)
            buffer.seek(0)

            if not output_name.lower().endswith('.pdf'):
                output_name += '.pdf'

            new_file = SharedFile.objects.create(
                name=output_name,
                uploaded_by=request.user,
                visibility='private',
                file_size=buffer.getbuffer().nbytes,
                file_type='application/pdf',
            )
            new_file.file.save(output_name, ContentFile(buffer.read()), save=True)

            try:
                new_file.file_hash = new_file.compute_hash()
                new_file.save(update_fields=['file_hash'])
            except Exception:
                pass

            messages.success(request, f'Merged PDF created: "{output_name}".')
        except Exception as e:
            messages.error(request, f'Could not merge PDFs: {e}')

        return redirect('pdf_tools_page')

def parse_page_ranges(raw, max_pages):
    selected = set()
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            start, end = part.split('-', 1)
            start = int(start)
            end = int(end)
            for i in range(start, end + 1):
                if 1 <= i <= max_pages:
                    selected.add(i)
        else:
            i = int(part)
            if 1 <= i <= max_pages:
                selected.add(i)
    return selected

class PDFRemovePagesView(LoginRequiredMixin, View):
    def post(self, request, pk):
        pdf = get_object_or_404(SharedFile, pk=pk, uploaded_by=request.user)

        if not getattr(pdf, 'is_pdf', False):
            messages.error(request, 'This file is not a PDF.')
            return redirect('pdf_tools_page')

        remove_pages_raw = request.POST.get('remove_pages', '').strip()
        output_name = request.POST.get('output_name', '').strip() or f'{pdf.name.rsplit(".", 1)[0]}-edited.pdf'

        try:
            reader = PdfReader(pdf.file.open('rb'))
            writer = PdfWriter()

            total_pages = len(reader.pages)
            remove_pages = parse_page_ranges(remove_pages_raw, total_pages)

            for page_num in range(1, total_pages + 1):
                if page_num not in remove_pages:
                    writer.add_page(reader.pages[page_num - 1])

            buffer = BytesIO()
            writer.write(buffer)
            buffer.seek(0)

            if not output_name.lower().endswith('.pdf'):
                output_name += '.pdf'

            new_file = SharedFile.objects.create(
                name=output_name,
                uploaded_by=request.user,
                visibility=pdf.visibility,
                folder=pdf.folder,
                description=f'Edited from {pdf.name}',
                tags=pdf.tags,
                file_size=buffer.getbuffer().nbytes,
                file_type='application/pdf',
            )
            new_file.file.save(output_name, ContentFile(buffer.read()), save=True)

            try:
                new_file.file_hash = new_file.compute_hash()
                new_file.save(update_fields=['file_hash'])
            except Exception:
                pass

            messages.success(request, f'Edited PDF created: "{output_name}".')
        except Exception as e:
            messages.error(request, f'Could not remove pages: {e}')

        return redirect('pdf_tools_page')

class PDFSplitView(LoginRequiredMixin, View):
    def post(self, request, pk):
        pdf = get_object_or_404(SharedFile, pk=pk, uploaded_by=request.user)

        if not getattr(pdf, 'is_pdf', False):
            messages.error(request, 'This file is not a PDF.')
            return redirect('pdf_tools_page')

        split_pages_raw = request.POST.get('split_pages', '').strip()

        try:
            reader = PdfReader(pdf.file.open('rb'))
            total_pages = len(reader.pages)
            split_pages = sorted(parse_page_ranges(split_pages_raw, total_pages))

            if not split_pages:
                messages.error(request, 'Please provide valid pages to extract.')
                return redirect('pdf_tools_page')

            writer = PdfWriter()
            for p in split_pages:
                writer.add_page(reader.pages[p - 1])

            output_name = f'{pdf.name.rsplit(".", 1)[0]}-split.pdf'
            buffer = BytesIO()
            writer.write(buffer)
            buffer.seek(0)

            new_file = SharedFile.objects.create(
                name=output_name,
                uploaded_by=request.user,
                visibility=pdf.visibility,
                folder=pdf.folder,
                description=f'Split from {pdf.name}',
                tags=pdf.tags,
                file_size=buffer.getbuffer().nbytes,
                file_type='application/pdf',
            )
            new_file.file.save(output_name, ContentFile(buffer.read()), save=True)

            try:
                new_file.file_hash = new_file.compute_hash()
                new_file.save(update_fields=['file_hash'])
            except Exception:
                pass

            messages.success(request, f'Split PDF created: "{output_name}".')
        except Exception as e:
            messages.error(request, f'Could not split PDF: {e}')

        return redirect('pdf_tools_page')

class PDFRotatePagesView(LoginRequiredMixin, View):
    def post(self, request, pk):
        pdf = get_object_or_404(SharedFile, pk=pk, uploaded_by=request.user)

        if not getattr(pdf, 'is_pdf', False):
            messages.error(request, 'This file is not a PDF.')
            return redirect('pdf_tools_page')

        rotate_pages_raw = request.POST.get('rotate_pages', '').strip()
        degrees = int(request.POST.get('degrees', '90'))

        try:
            reader = PdfReader(pdf.file.open('rb'))
            writer = PdfWriter()
            total_pages = len(reader.pages)
            rotate_pages = parse_page_ranges(rotate_pages_raw, total_pages)

            for page_num in range(1, total_pages + 1):
                page = reader.pages[page_num - 1]
                if page_num in rotate_pages:
                    page.rotate(degrees)
                writer.add_page(page)

            output_name = f'{pdf.name.rsplit(".", 1)[0]}-rotated.pdf'
            buffer = BytesIO()
            writer.write(buffer)
            buffer.seek(0)

            new_file = SharedFile.objects.create(
                name=output_name,
                uploaded_by=request.user,
                visibility=pdf.visibility,
                folder=pdf.folder,
                description=f'Rotated from {pdf.name}',
                tags=pdf.tags,
                file_size=buffer.getbuffer().nbytes,
                file_type='application/pdf',
            )
            new_file.file.save(output_name, ContentFile(buffer.read()), save=True)

            try:
                new_file.file_hash = new_file.compute_hash()
                new_file.save(update_fields=['file_hash'])
            except Exception:
                pass

            messages.success(request, f'Rotated PDF created: "{output_name}".')
        except Exception as e:
            messages.error(request, f'Could not rotate PDF: {e}')

        return redirect('pdf_tools_page')

class PDFReorderPagesView(LoginRequiredMixin, View):
    def post(self, request, pk):
        pdf = get_object_or_404(SharedFile, pk=pk, uploaded_by=request.user)

        if not getattr(pdf, 'is_pdf', False):
            messages.error(request, 'This file is not a PDF.')
            return redirect('pdf_tools_page')

        page_order_raw = request.POST.get('page_order', '').strip()

        try:
            reader = PdfReader(pdf.file.open('rb'))
            total_pages = len(reader.pages)

            page_order = [int(x.strip()) for x in page_order_raw.split(',') if x.strip()]
            if sorted(page_order) != list(range(1, total_pages + 1)):
                messages.error(request, 'Page order must include every page exactly once.')
                return redirect('pdf_tools_page')

            writer = PdfWriter()
            for page_num in page_order:
                writer.add_page(reader.pages[page_num - 1])

            output_name = f'{pdf.name.rsplit(".", 1)[0]}-reordered.pdf'
            buffer = BytesIO()
            writer.write(buffer)
            buffer.seek(0)

            new_file = SharedFile.objects.create(
                name=output_name,
                uploaded_by=request.user,
                visibility=pdf.visibility,
                folder=pdf.folder,
                description=f'Reordered from {pdf.name}',
                tags=pdf.tags,
                file_size=buffer.getbuffer().nbytes,
                file_type='application/pdf',
            )
            new_file.file.save(output_name, ContentFile(buffer.read()), save=True)

            try:
                new_file.file_hash = new_file.compute_hash()
                new_file.save(update_fields=['file_hash'])
            except Exception:
                pass

            messages.success(request, f'Reordered PDF created: "{output_name}".')
        except Exception as e:
            messages.error(request, f'Could not reorder pages: {e}')

        return redirect('pdf_tools_page')

class PDFToolsPageView(LoginRequiredMixin, TemplateView):
    template_name = 'files/pdf_tools.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user

        all_mine = _visible_files_qs(user).filter(uploaded_by=user).order_by('-created_at')
        pdf_files = [f for f in all_mine if getattr(f, 'is_pdf', False)]

        ctx.update({
            'pdf_files': pdf_files,
        })
        return ctx

class PDFMergeImagesView(LoginRequiredMixin, View):
    def post(self, request):
        ids = request.POST.getlist('multi_files')

        if not ids:
            messages.error(request, 'Select images first.')
            return redirect('convert_pdf_page')

        images = SharedFile.objects.filter(id__in=ids, uploaded_by=request.user)

        from PIL import Image
        from io import BytesIO

        pil_images = []

        for img_file in images:
            img = Image.open(img_file.file)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            pil_images.append(img)

        buffer = BytesIO()
        pil_images[0].save(buffer, format='PDF', save_all=True, append_images=pil_images[1:])
        buffer.seek(0)

        name = 'combined-images.pdf'

        new = SharedFile.objects.create(
            name=name,
            uploaded_by=request.user,
            visibility='private',
            file_type='application/pdf',
            file_size=buffer.getbuffer().nbytes,
        )
        new.file.save(name, ContentFile(buffer.read()), save=True)

        messages.success(request, 'Images combined into PDF successfully.')
        return redirect('convert_pdf_page')

# ─────────────────────────────────────────────────────────────────────────────
# Quick Sign — standalone PDF signing tool (no flow, no other signers)
# ─────────────────────────────────────────────────────────────────────────────

class QuickSignView(LoginRequiredMixin, View):
    """
    Lets a user sign any of their PDF files directly — no signature request
    flow, no other signers, no emails.  The signed copy is saved alongside
    the original as a new SharedFile.
    """
    template_name = 'files/quick_sign.html'

    def _build_file_tree(self, user):
        """
        Returns a structure for the file picker:
          - root_pdfs:   PDF files not in any folder
          - folders:     list of { folder, pdfs }  (only folders that contain PDFs)
        """
        all_pdfs = [
            f for f in _visible_files_qs(user).filter(uploaded_by=user)
                                              .select_related('folder')
                                              .order_by('name')
            if getattr(f, 'is_pdf', False)
        ]
        root_pdfs = [f for f in all_pdfs if f.folder_id is None]

        # Group the rest by folder
        from collections import defaultdict
        by_folder = defaultdict(list)
        for f in all_pdfs:
            if f.folder_id:
                by_folder[f.folder_id].append(f)

        folders = []
        for folder in _visible_folders_qs(user).filter(
            pk__in=by_folder.keys()
        ).order_by('name'):
            folders.append({
                'folder': folder,
                'pdfs':   by_folder[folder.pk],
            })

        return root_pdfs, folders

    def get(self, request, pk=None):
        selected = None
        if pk:
            selected = get_object_or_404(SharedFile, pk=pk, uploaded_by=request.user)
            if not getattr(selected, 'is_pdf', False):
                messages.error(request, 'Only PDF files can be quick-signed.')
                return redirect('quick_sign')

        root_pdfs, folders = self._build_file_tree(request.user)
        return render(request, self.template_name, {
            'root_pdfs': root_pdfs,
            'folders':   folders,
            'selected':  selected,
        })

    def post(self, request, pk=None):
        file_id   = request.POST.get('file_id') or (str(pk) if pk else None)
        sig_data  = request.POST.get('signature_data', '').strip()
        sig_type  = request.POST.get('signature_type', 'draw')
        sig_page  = int(request.POST.get('sig_page', 1))
        sig_x     = float(request.POST.get('sig_x', 10))
        sig_y     = float(request.POST.get('sig_y', 80))
        sig_w     = float(request.POST.get('sig_w', 35))
        sig_h     = float(request.POST.get('sig_h', 12))
        output_name = request.POST.get('output_name', '').strip()

        if not file_id or not sig_data:
            messages.error(request, 'Please select a file and provide your signature.')
            return redirect('quick_sign')

        pdf = get_object_or_404(SharedFile, pk=file_id, uploaded_by=request.user)
        if not getattr(pdf, 'is_pdf', False):
            messages.error(request, 'Only PDF files can be quick-signed.')
            return redirect('quick_sign')

        try:
            # ── Build a minimal SignatureField-like object for _embed helper ──
            from dataclasses import dataclass
            from datetime import datetime as _dt

            @dataclass
            class _FakeField:
                field_type:  str
                value:       str
                page:        int
                x_pct:       float
                y_pct:       float
                width_pct:   float
                height_pct:  float
                signer:      object
                filled_at:   object

            @dataclass
            class _FakeSigner:
                name: str

            fake_signer = _FakeSigner(name=request.user.full_name)
            fake_field  = _FakeField(
                field_type = 'signature',
                value      = sig_data,
                page       = sig_page,
                x_pct      = sig_x,
                y_pct      = sig_y,
                width_pct  = sig_w,
                height_pct = sig_h,
                signer     = fake_signer,
                filled_at  = timezone.now(),
            )

            # ── Embed the signature directly using the reportlab helper ──────
            try:
                from reportlab.pdfgen import canvas as rl_canvas
                from reportlab.lib.utils import ImageReader
            except ImportError:
                messages.error(request, 'reportlab is not installed on this server.')
                return redirect('quick_sign')

            import base64
            from collections import defaultdict

            pdf_bytes = pdf.file.open('rb').read()
            reader = PdfReader(BytesIO(pdf_bytes))
            writer = PdfWriter()

            # Colour constants (same as _embed_signatures_in_pdf)
            DS_BLUE       = (0.098, 0.376, 0.875)
            DS_BLUE_LIGHT = (0.918, 0.937, 0.992)
            DS_INK        = (0.063, 0.271, 0.773)
            DS_LABEL      = (0.373, 0.420, 0.510)
            DS_CHECK      = (0.063, 0.271, 0.773)
            STRIP_H       = 18.0

            from datetime import datetime as _dt2
            signed_at = _dt2.now().strftime('%d %b %Y %H:%M UTC')

            for page_idx in range(len(reader.pages)):
                page    = reader.pages[page_idx]
                page_w  = float(page.mediabox.width)
                page_h  = float(page.mediabox.height)
                page_num = page_idx + 1

                overlay = BytesIO()
                c = rl_canvas.Canvas(overlay, pagesize=(page_w, page_h))

                # ── Header strip (all pages) ──────────────────────────────
                hdr_y = page_h - STRIP_H
                c.setFillColorRGB(0.937, 0.953, 1.0)
                c.rect(0, hdr_y, page_w, STRIP_H, stroke=0, fill=1)
                c.setStrokeColorRGB(*DS_BLUE)
                c.setLineWidth(0.8)
                c.line(0, hdr_y, page_w, hdr_y)
                c.setFont('Helvetica-Bold', 6.5)
                c.setFillColorRGB(*DS_BLUE)
                c.drawString(5, hdr_y + STRIP_H * 0.28, '\u26BF EasyOffice')
                c.setFont('Helvetica', 6.0)
                c.setFillColorRGB(*DS_LABEL)
                c.drawString(52, hdr_y + STRIP_H * 0.28, f'\u00B7  {pdf.name}')
                c.drawRightString(page_w - 5, hdr_y + STRIP_H * 0.28,
                                  f'Page {page_num} / {len(reader.pages)}')

                # ── Footer strip (all pages) ──────────────────────────────
                c.setFillColorRGB(0.937, 0.953, 1.0)
                c.rect(0, 0, page_w, STRIP_H, stroke=0, fill=1)
                c.setLineWidth(0.8)
                c.line(0, STRIP_H, page_w, STRIP_H)
                c.setFont('Helvetica-Bold', 5.5)
                c.setFillColorRGB(*DS_BLUE)
                c.drawString(5, STRIP_H * 0.28, 'Signed by:')
                c.setFont('Helvetica', 5.5)
                c.setFillColorRGB(0.2, 0.2, 0.2)
                c.drawString(38, STRIP_H * 0.28, request.user.full_name)
                c.drawRightString(page_w - 5, STRIP_H * 0.28,
                                  f'Electronically signed \u00B7 {signed_at}')

                # ── Signature field (only on selected page) ───────────────
                if page_num == sig_page:
                    fx = (sig_x  / 100.0) * page_w
                    fw = (sig_w  / 100.0) * page_w
                    fh = (sig_h  / 100.0) * page_h
                    fy = page_h * (1.0 - (sig_y + sig_h) / 100.0)

                    label_h = max(9.0, min(fh * 0.24, 16.0)) if fh >= 20 else 0.0
                    sig_h_area = fh - label_h
                    sig_y_area = fy + label_h

                    # Background + border
                    c.setFillColorRGB(*DS_BLUE_LIGHT)
                    c.setStrokeColorRGB(*DS_BLUE)
                    c.setLineWidth(0.6)
                    c.roundRect(fx, fy, fw, fh, radius=2, stroke=1, fill=1)
                    if label_h:
                        c.setLineWidth(1.0)
                        c.line(fx + 2, fy + label_h, fx + fw - 2, fy + label_h)

                    # Drawn / uploaded
                    if sig_data.startswith('data:image'):
                        try:
                            _, b64 = sig_data.split(',', 1)
                            raw = base64.b64decode(b64)
                            img = Image.open(BytesIO(raw)).convert('RGBA')
                            bg  = Image.new('RGBA', img.size, (255, 255, 255, 255))
                            bg.paste(img, mask=img)
                            bg  = bg.convert('RGB')
                            ib  = BytesIO(); bg.save(ib, format='PNG'); ib.seek(0)
                            pad = 5
                            c.drawImage(ImageReader(ib),
                                        fx + pad, sig_y_area + pad,
                                        width=fw - pad*2, height=sig_h_area - pad*2,
                                        preserveAspectRatio=True, anchor='c', mask='auto')
                        except Exception:
                            pass

                    # Typed signature — render via PIL for cursive font
                    elif sig_type == 'type':
                        display_text = sig_data
                        font_hint    = 'Dancing Script'
                        if sig_data.startswith('font:') and '|' in sig_data:
                            parts        = sig_data.split('|', 1)
                            font_hint    = parts[0][5:]
                            display_text = parts[1]
                        img_buf = None
                        try:
                            img_buf = _typed_sig_to_image(display_text, fw, sig_h_area, font_name=font_hint)
                        except Exception:
                            pass
                        if img_buf:
                            pad = 4
                            c.drawImage(ImageReader(img_buf),
                                        fx + pad, sig_y_area + pad,
                                        width=fw - pad*2, height=sig_h_area - pad*2,
                                        preserveAspectRatio=True, anchor='c', mask='auto')
                        else:
                            fs = max(8.0, min(sig_h_area * 0.62, 30.0))
                            c.setFont('Helvetica-BoldOblique', fs)
                            c.setFillColorRGB(*DS_INK)
                            c.drawString(fx + 6, sig_y_area + (sig_h_area - fs) * 0.4, display_text)

                    # Label strip
                    if label_h:
                        lf = max(5.5, min(label_h * 0.50, 7.5))
                        my = fy + (label_h - lf) * 0.45
                        c.setFont('Helvetica-Bold', lf)
                        c.setFillColorRGB(*DS_CHECK)
                        c.drawString(fx + 3, my, '\u2713')
                        c.setFont('Helvetica', lf)
                        c.setFillColorRGB(*DS_LABEL)
                        c.saveState()
                        p = c.beginPath(); p.rect(fx + 3, fy, fw * 0.62, label_h)
                        c.clipPath(p, stroke=0, fill=0)
                        c.drawString(fx + 3 + lf, my, f' {request.user.full_name[:24]}')
                        c.restoreState()
                        c.setFont('Helvetica', lf)
                        c.setFillColorRGB(*DS_LABEL)
                        c.drawRightString(fx + fw - 4, my, signed_at[:11])

                c.save()
                overlay.seek(0)
                page.merge_page(PdfReader(overlay).pages[0])
                writer.add_page(page)

            # ── Save signed copy ─────────────────────────────────────────
            out = BytesIO()
            writer.write(out)
            signed_bytes = out.getvalue()

            base_name = pdf.name[:-4] if pdf.name.lower().endswith('.pdf') else pdf.name
            signed_name = output_name if output_name else f'{base_name}-signed.pdf'
            if not signed_name.lower().endswith('.pdf'):
                signed_name += '.pdf'

            signed_file = SharedFile.objects.create(
                name        = signed_name,
                uploaded_by = request.user,
                folder      = pdf.folder,
                visibility  = pdf.visibility,
                description = f'Quick-signed copy of "{pdf.name}" by {request.user.full_name}',
                tags        = pdf.tags,
                file_size   = len(signed_bytes),
                file_type   = 'application/pdf',
            )
            signed_file.file.save(signed_name, ContentFile(signed_bytes), save=True)
            try:
                signed_file.file_hash = signed_file.compute_hash()
                signed_file.save(update_fields=['file_hash'])
            except Exception:
                pass

            messages.success(request, f'Signed copy saved as "{signed_name}".')
            return redirect('file_manager')

        except Exception as e:
            messages.error(request, f'Signing failed: {e}')
            return redirect('quick_sign')


# ─────────────────────────────────────────────────────────────────────────────
# NOTE EDITOR → PDF
# ─────────────────────────────────────────────────────────────────────────────

def _build_note_pdf(title, content, user):
    """Render a plain-text / markdown-lite note to a clean PDF using reportlab."""
    import re
    from datetime import datetime
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_JUSTIFY
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, HRFlowable
    )

    buf = BytesIO()
    PAGE_W, PAGE_H = A4
    MARGIN = 22 * mm

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        rightMargin=MARGIN, leftMargin=MARGIN,
        topMargin=28*mm, bottomMargin=20*mm,
        title=title, author=getattr(user, 'full_name', str(user)),
    )

    C_INK    = colors.HexColor('#1e293b')
    C_ACCENT = colors.HexColor('#3b82f6')
    C_MUTED  = colors.HexColor('#64748b')
    C_HEAD   = colors.HexColor('#1e3a5f')

    base = getSampleStyleSheet()
    sTitle  = ParagraphStyle('NoteTitle', parent=base['Title'],
                fontSize=24, textColor=C_HEAD, spaceAfter=2*mm, leading=30)
    sMeta   = ParagraphStyle('NoteMeta',  parent=base['Normal'],
                fontSize=9, textColor=C_MUTED, spaceAfter=5*mm)
    sH1     = ParagraphStyle('NoteH1',    parent=base['Heading1'],
                fontSize=17, textColor=C_HEAD, spaceBefore=7*mm, spaceAfter=3*mm)
    sH2     = ParagraphStyle('NoteH2',    parent=base['Heading2'],
                fontSize=13, textColor=C_HEAD, spaceBefore=5*mm, spaceAfter=2*mm)
    sBody   = ParagraphStyle('NoteBody',  parent=base['Normal'],
                fontSize=11, leading=17, spaceAfter=3*mm, alignment=TA_JUSTIFY,
                textColor=C_INK)
    sBullet = ParagraphStyle('NoteBullet', parent=base['Normal'],
                fontSize=11, leading=16, spaceAfter=2*mm,
                leftIndent=12*mm, firstLineIndent=-6*mm, textColor=C_INK)
    sCode   = ParagraphStyle('NoteCode',  parent=base['Normal'],
                fontName='Courier', fontSize=9, leading=14, spaceAfter=3*mm,
                leftIndent=6*mm, backColor=colors.HexColor('#f1f5f9'),
                textColor=colors.HexColor('#0f172a'))

    def inline(text):
        """Bold, italic, inline-code → reportlab XML."""
        text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        text = re.sub(r'\*(.+?)\*',     r'<i>\1</i>', text)
        text = re.sub(r'`(.+?)`',
                      r'<font name="Courier" color="#0f172a">\1</font>', text)
        return text

    story = []
    story.append(Paragraph(title, sTitle))
    author_name = getattr(user, 'full_name', str(user))
    story.append(Paragraph(
        f'{author_name}  ·  {datetime.now().strftime("%d %B %Y, %H:%M")}', sMeta))
    story.append(HRFlowable(width='100%', color=C_ACCENT,
                             thickness=1.5, spaceAfter=6*mm))

    lines = content.splitlines()
    buf_lines = []

    def flush():
        if buf_lines:
            text = ' '.join(l for l in buf_lines if l)
            if text:
                story.append(Paragraph(inline(text), sBody))
            buf_lines.clear()

    in_code = False
    code_block = []

    for line in lines:
        stripped = line.strip()

        # Fenced code block
        if stripped.startswith('```'):
            if not in_code:
                flush()
                in_code = True
                code_block = []
            else:
                in_code = False
                story.append(Paragraph(
                    '<br/>'.join(code_block) or ' ', sCode))
                code_block = []
            continue
        if in_code:
            code_block.append(
                line.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))
            continue

        if stripped.startswith('# '):
            flush(); story.append(Paragraph(inline(stripped[2:]), sH1))
        elif stripped.startswith('## '):
            flush(); story.append(Paragraph(inline(stripped[3:]), sH2))
        elif stripped.startswith('### '):
            flush(); story.append(Paragraph(inline(stripped[4:]), sH2))
        elif stripped.startswith(('- ', '* ', '• ')):
            flush()
            story.append(Paragraph(f'•  {inline(stripped[2:])}', sBullet))
        elif re.match(r'^\d+\.\s', stripped):
            flush()
            num, rest = stripped.split('.', 1)
            story.append(Paragraph(f'{num}.  {inline(rest.strip())}', sBullet))
        elif stripped == '':
            flush()
            if story:
                story.append(Spacer(1, 2*mm))
        else:
            buf_lines.append(stripped)

    flush()

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(C_MUTED)
        canvas.drawString(MARGIN, 12*mm, f'EasyOffice · {title}')
        canvas.drawRightString(PAGE_W - MARGIN, 12*mm, f'Page {doc.page}')
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


class NotesToPDFView(LoginRequiredMixin, View):
    template_name = 'files/notes_editor.html'

    def get(self, request):
        folders_qs = _visible_folders_qs(request.user).order_by('name')
        return render(request, self.template_name, {'folders': folders_qs})

    def post(self, request):
        title   = request.POST.get('title', '').strip() or 'Untitled Note'
        content = request.POST.get('content', '').strip()
        folder_id = request.POST.get('folder_id', '').strip()

        if not content:
            messages.error(request, 'Please write something before saving.')
            return redirect('notes_to_pdf')

        try:
            pdf_bytes = _build_note_pdf(title, content, request.user)
        except Exception as e:
            messages.error(request, f'PDF generation failed: {e}')
            return redirect('notes_to_pdf')

        safe_title = re.sub(r'[^\w\s-]', '', title)[:50].strip().replace(' ', '-')
        filename   = f'{safe_title}.pdf'

        folder = None
        if folder_id:
            try:
                folder = _visible_folders_qs(request.user).get(pk=folder_id)
            except Exception:
                pass

        new_file = SharedFile.objects.create(
            name        = filename,
            uploaded_by = request.user,
            folder      = folder,
            visibility  = 'private',
            description = f'Note: {title}',
            file_type   = 'application/pdf',
            file_size   = len(pdf_bytes),
        )
        new_file.file.save(filename, ContentFile(pdf_bytes), save=True)
        try:
            new_file.file_hash = new_file.compute_hash()
            new_file.save(update_fields=['file_hash'])
        except Exception:
            pass

        messages.success(request, f'Note saved as "{filename}".')
        return redirect('file_manager')


# ─────────────────────────────────────────────────────────────────────────────
# PDF → WORD  &  PDF → IMAGE  (shared PDF picker helper)
# ─────────────────────────────────────────────────────────────────────────────

def _get_user_pdf_tree(user):
    """Return (root_pdfs, folders_with_pdfs) for the PDF picker."""
    from collections import defaultdict
    all_pdfs = [
        f for f in _visible_files_qs(user).filter(uploaded_by=user)
                                          .select_related('folder')
                                          .order_by('name')
        if getattr(f, 'is_pdf', False)
    ]
    root_pdfs = [f for f in all_pdfs if f.folder_id is None]
    by_folder = defaultdict(list)
    for f in all_pdfs:
        if f.folder_id:
            by_folder[f.folder_id].append(f)
    folders = []
    for folder in _visible_folders_qs(user).filter(
            pk__in=by_folder.keys()).order_by('name'):
        folders.append({'folder': folder, 'pdfs': by_folder[folder.pk]})
    return root_pdfs, folders


def _pdf_to_images_bytes(pdf_path, fmt='png', dpi=150):
    """
    Convert a PDF to per-page images. Returns list of (page_num, bytes) tuples.
    Tries: pdf2image → pdftoppm → LibreOffice.
    """
    out_dir = tempfile.mkdtemp()
    images  = []

    # ── pdf2image (pip install pdf2image) ────────────────────────────────────
    try:
        from pdf2image import convert_from_path
        pages = convert_from_path(pdf_path, dpi=dpi, fmt=fmt)
        for i, page in enumerate(pages, 1):
            buf = BytesIO()
            page.save(buf, format='PNG' if fmt == 'png' else 'JPEG')
            images.append((i, buf.getvalue()))
        return images
    except ImportError:
        pass

    # ── pdftoppm (poppler-utils) ──────────────────────────────────────────────
    try:
        prefix  = os.path.join(out_dir, 'page')
        fmt_flag = '-png' if fmt == 'png' else '-jpeg'
        result  = subprocess.run(
            ['pdftoppm', fmt_flag, '-r', str(dpi), pdf_path, prefix],
            capture_output=True, timeout=180)
        if result.returncode == 0:
            files = sorted(f for f in os.listdir(out_dir)
                           if f.startswith('page'))
            for i, fn in enumerate(files, 1):
                with open(os.path.join(out_dir, fn), 'rb') as fh:
                    images.append((i, fh.read()))
            if images:
                return images
    except FileNotFoundError:
        pass

    # ── LibreOffice fallback ──────────────────────────────────────────────────
    result = subprocess.run(
        ['libreoffice', '--headless', '--convert-to', fmt,
         '--outdir', out_dir, pdf_path],
        capture_output=True, text=True, timeout=180)
    files = sorted(f for f in os.listdir(out_dir) if f.endswith(f'.{fmt}'))
    if not files:
        raise RuntimeError(
            'PDF→Image conversion requires poppler-utils or pdf2image. '
            'Run: sudo apt install poppler-utils  OR  pip install pdf2image')
    for i, fn in enumerate(files, 1):
        with open(os.path.join(out_dir, fn), 'rb') as fh:
            images.append((i, fh.read()))
    return images


class PDFToWordView(LoginRequiredMixin, View):
    template_name = 'files/pdf_converter.html'

    def get(self, request):
        root_pdfs, folders = _get_user_pdf_tree(request.user)
        return render(request, self.template_name, {
            'root_pdfs': root_pdfs, 'folders': folders,
            'active_tab': 'word',
        })

    def post(self, request):
        file_id = request.POST.get('file_id', '').strip()
        if not file_id:
            messages.error(request, 'Please select a PDF.')
            return redirect('pdf_to_word')

        pdf = get_object_or_404(SharedFile, pk=file_id, uploaded_by=request.user)
        if not getattr(pdf, 'is_pdf', False):
            messages.error(request, 'Only PDF files can be converted.')
            return redirect('pdf_to_word')

        try:
            import shutil

            src = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
            src.write(pdf.file.open('rb').read())
            src.flush(); src.close()

            out_dir = tempfile.mkdtemp()

            # ── Step 1: PDF → ODT via writer_pdf_import ───────────────────────
            # Direct PDF→DOCX fails because LibreOffice opens PDFs as Draw
            # documents by default. writer_pdf_import opens it as Writer (text).
            r1 = subprocess.run(
                [
                    'libreoffice', '--headless', '--norestore',
                    '--infilter=writer_pdf_import',
                    '--convert-to', 'odt',
                    '--outdir', out_dir,
                    src.name,
                ],
                capture_output=True, text=True, timeout=120,
            )
            os.unlink(src.name)

            odt_files = [f for f in os.listdir(out_dir) if f.endswith('.odt')]
            if not odt_files:
                # Combine stdout+stderr for a useful error message
                lo_err = (r1.stdout + r1.stderr).strip()
                raise RuntimeError(
                    lo_err or 'LibreOffice produced no ODT output in step 1.')

            odt_path = os.path.join(out_dir, odt_files[0])

            # ── Step 2: ODT → DOCX ────────────────────────────────────────────
            r2 = subprocess.run(
                [
                    'libreoffice', '--headless', '--norestore',
                    '--convert-to', 'docx:MS Word 2007 XML',
                    '--outdir', out_dir,
                    odt_path,
                ],
                capture_output=True, text=True, timeout=120,
            )

            docx_files = [f for f in os.listdir(out_dir) if f.endswith('.docx')]
            if not docx_files:
                lo_err = (r2.stdout + r2.stderr).strip()
                raise RuntimeError(
                    lo_err or 'LibreOffice produced no DOCX output in step 2.')

            docx_path = os.path.join(out_dir, docx_files[0])
            with open(docx_path, 'rb') as fh:
                docx_bytes = fh.read()

            base_name = pdf.name[:-4] if pdf.name.lower().endswith('.pdf') else pdf.name
            out_name  = f'{base_name}.docx'
            MIME_DOCX = ('application/vnd.openxmlformats-officedocument'
                         '.wordprocessingml.document')

            new_file = SharedFile.objects.create(
                name        = out_name,
                uploaded_by = request.user,
                folder      = pdf.folder,
                visibility  = pdf.visibility,
                description = f'Converted from PDF: {pdf.name}',
                file_type   = MIME_DOCX,
                file_size   = len(docx_bytes),
            )
            new_file.file.save(out_name, ContentFile(docx_bytes), save=True)

            shutil.rmtree(out_dir, ignore_errors=True)
            messages.success(request, f'"{pdf.name}" converted → "{out_name}".')
            return redirect('file_manager')

        except Exception as e:
            messages.error(request, f'Conversion failed: {e}')
            return redirect('pdf_to_word')


class PDFToImageView(LoginRequiredMixin, View):
    template_name = 'files/pdf_converter.html'

    def get(self, request):
        root_pdfs, folders = _get_user_pdf_tree(request.user)
        return render(request, self.template_name, {
            'root_pdfs': root_pdfs, 'folders': folders,
            'active_tab': 'image',
        })

    def post(self, request):
        file_id = request.POST.get('file_id', '').strip()
        fmt     = request.POST.get('fmt', 'png').lower()
        dpi     = int(request.POST.get('dpi', 150))
        if fmt not in ('png', 'jpg'):  fmt = 'png'
        if dpi not in (72, 96, 150, 300): dpi = 150

        if not file_id:
            messages.error(request, 'Please select a PDF.')
            return redirect('pdf_to_image')

        pdf = get_object_or_404(SharedFile, pk=file_id, uploaded_by=request.user)
        if not getattr(pdf, 'is_pdf', False):
            messages.error(request, 'Only PDF files can be converted.')
            return redirect('pdf_to_image')

        try:
            src = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
            src.write(pdf.file.open('rb').read())
            src.flush(); src.close()

            page_images = _pdf_to_images_bytes(src.name, fmt=fmt, dpi=dpi)
            os.unlink(src.name)

            if not page_images:
                raise RuntimeError('No pages were converted.')

            base_name = pdf.name[:-4] if pdf.name.lower().endswith('.pdf') else pdf.name
            mime = 'image/png' if fmt == 'png' else 'image/jpeg'

            for page_num, img_bytes in page_images:
                suffix = f'-p{page_num:03d}.{fmt}'
                out_name = f'{base_name}{suffix}'
                new_file = SharedFile.objects.create(
                    name        = out_name,
                    uploaded_by = request.user,
                    folder      = pdf.folder,
                    visibility  = pdf.visibility,
                    description = f'Page {page_num} of "{pdf.name}"',
                    file_type   = mime,
                    file_size   = len(img_bytes),
                )
                new_file.file.save(out_name, ContentFile(img_bytes), save=True)

            n = len(page_images)
            messages.success(
                request,
                f'"{pdf.name}" → {n} image{"s" if n>1 else ""} saved to your files.')
            return redirect('file_manager')

        except Exception as e:
            messages.error(request, f'Conversion failed: {e}')
            return redirect('pdf_to_image')