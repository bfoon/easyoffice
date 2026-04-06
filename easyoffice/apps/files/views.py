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
import os, subprocess, tempfile, hashlib, mimetypes
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
        Q(shared_with=user)
    ).distinct()


def _visible_folders_qs(user):
    profile = getattr(user, 'staffprofile', None)
    unit = profile.unit if profile else None
    return FileFolder.objects.filter(
        Q(owner=user) | Q(visibility='office') | Q(visibility='unit', unit=unit)
    ).distinct()


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



def _add_certificate_page(writer, sig_req):
    """Append a Certificate of Completion page to the PDF writer."""
    import io
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import A4
    from pypdf import PdfReader

    pw, ph = A4
    packet = io.BytesIO()
    c = rl_canvas.Canvas(packet, pagesize=A4)

    c.setFillColorRGB(0.07, 0.15, 0.40)
    c.rect(0, ph - 72, pw, 72, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont('Helvetica-Bold', 16)
    c.drawString(36, ph - 36, 'Certificate of Completion')
    c.setFont('Helvetica', 9)
    c.setFillColorRGB(0.65, 0.75, 0.95)
    c.drawString(36, ph - 56, 'Electronic Signature Record — EasyOffice')

    y = ph - 100

    def row(lbl, val, cy):
        c.setFont('Helvetica-Bold', 7)
        c.setFillColorRGB(0.45, 0.45, 0.58)
        c.drawString(36, cy, lbl.upper())
        c.setFont('Helvetica', 10)
        c.setFillColorRGB(0.08, 0.08, 0.20)
        c.drawString(36, cy - 13, str(val)[:90])
        return cy - 30

    y = row('Document', sig_req.title, y)
    y = row('Request ID', str(sig_req.id), y)
    sent_by = getattr(sig_req.created_by, 'full_name', str(sig_req.created_by))
    y = row('Initiated by', sent_by, y)
    if sig_req.completed_at:
        y = row('Completed', sig_req.completed_at.strftime('%B %d, %Y at %H:%M UTC'), y)
    y -= 8
    c.setStrokeColorRGB(0.80, 0.82, 0.92)
    c.setLineWidth(0.5)
    c.line(36, y, pw - 36, y)
    y -= 20

    c.setFont('Helvetica-Bold', 11)
    c.setFillColorRGB(0.07, 0.15, 0.40)
    c.drawString(36, y, 'Signers')
    y -= 16

    for signer in sig_req.signers.all():
        if y < 120:
            break
        box_h = 70
        c.setFillColorRGB(0.96, 0.97, 1.0)
        c.roundRect(36, y - box_h, pw - 72, box_h, 5, fill=1, stroke=0)
        if signer.status == 'signed':
            c.setFillColorRGB(0.06, 0.62, 0.45)
        elif signer.status == 'declined':
            c.setFillColorRGB(0.75, 0.10, 0.10)
        else:
            c.setFillColorRGB(0.60, 0.60, 0.70)
        c.circle(52, y - box_h / 2, 5, fill=1, stroke=0)
        c.setFillColorRGB(0.05, 0.08, 0.22)
        c.setFont('Helvetica-Bold', 10)
        c.drawString(66, y - 16, signer.name)
        c.setFont('Helvetica', 9)
        c.setFillColorRGB(0.35, 0.35, 0.50)
        c.drawString(66, y - 29, signer.email)
        status_line = signer.get_status_display()
        if signer.signed_at:
            status_line += '  |  ' + signer.signed_at.strftime('%b %d, %Y %H:%M UTC')
        if signer.ip_address:
            status_line += '  |  IP ' + signer.ip_address
        c.setFont('Helvetica', 8)
        c.setFillColorRGB(0.50, 0.50, 0.62)
        c.drawString(66, y - 42, status_line[:90])
        c.setFont('Helvetica-Bold', 7)
        c.setFillColorRGB(0.18, 0.39, 0.75)
        c.drawString(66, y - 56, 'Signer #' + str(signer.order))
        y -= box_h + 8

    y -= 6
    c.setStrokeColorRGB(0.80, 0.82, 0.92)
    c.line(36, y, pw - 36, y)
    y -= 18

    c.setFont('Helvetica-Bold', 11)
    c.setFillColorRGB(0.07, 0.15, 0.40)
    c.drawString(36, y, 'Audit Trail')
    y -= 16
    for event in sig_req.audit_trail.all()[:18]:
        if y < 80:
            break
        c.setFillColorRGB(0.18, 0.39, 0.75)
        c.circle(43, y - 3, 3, fill=1, stroke=0)
        c.setFont('Helvetica-Bold', 8)
        c.setFillColorRGB(0.08, 0.08, 0.22)
        c.drawString(54, y - 5, event.get_event_display())
        c.setFont('Helvetica', 8)
        c.setFillColorRGB(0.42, 0.42, 0.55)
        detail = event.timestamp.strftime('%b %d, %Y %H:%M UTC')
        if event.signer_name:
            detail = event.signer_name + '  |  ' + detail
        if event.ip_address:
            detail += '  |  ' + event.ip_address
        c.drawString(200, y - 5, detail[:70])
        y -= 14

    if sig_req.audit_hash:
        y -= 6
        c.setFont('Helvetica', 7)
        c.setFillColorRGB(0.52, 0.52, 0.65)
        c.drawString(36, y, 'Tamper-Evident Audit Hash (SHA-256):')
        c.setFont('Courier', 7)
        c.drawString(36, y - 11, sig_req.audit_hash)

    c.setFillColorRGB(0.07, 0.15, 0.40)
    c.rect(0, 0, pw, 36, fill=1, stroke=0)
    c.setFillColorRGB(0.65, 0.72, 0.92)
    c.setFont('Helvetica', 8)
    c.drawString(36, 13, 'Generated by EasyOffice · Electronic Signature System')
    from django.utils import timezone as _tz
    c.drawRightString(pw - 36, 13, 'Generated: ' + _tz.now().strftime('%Y-%m-%d %H:%M UTC'))

    c.save()
    packet.seek(0)
    writer.add_page(PdfReader(packet).pages[0])


def _embed_signatures_in_pdf(sig_req):
    """
    Embed all filled signature fields into the PDF and save to
    sig_req.signed_document.  Handles both cases:
      - Fields were placed: embed each field value at its exact coordinates.
      - No fields (free signing): stamp each signer's signature on the last page.
    Requires: pypdf, reportlab
    """
    import io, base64, os, tempfile, logging
    from collections import defaultdict
    from django.core.files import File as _DFile

    log = logging.getLogger(__name__)

    try:
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.lib.utils import ImageReader
    except ImportError as exc:
        log.error('PDF signing skipped — missing library: %s', exc)
        return

    doc = sig_req.document
    if not doc or not getattr(doc, 'file', None):
        log.warning('SignatureRequest %s has no document file.', sig_req.id)
        return

    try:
        reader = PdfReader(doc.file.open('rb'))
    except Exception as exc:
        log.error('Cannot open PDF for request %s: %s', sig_req.id, exc)
        return

    total_pages = len(reader.pages)

    # Group filled field values by page
    fields_by_page = defaultdict(list)
    for field in sig_req.fields.exclude(value='').select_related('signer'):
        fields_by_page[field.page].append(field)

    # Signers who signed without any field placement — stamp on last page
    free_signers = [
        s for s in sig_req.signers.filter(status='signed')
        if s.signature_data and not s.fields.exists()
    ]

    writer = PdfWriter()

    for page_num, page in enumerate(reader.pages, start=1):
        pw = float(page.mediabox.width)
        ph = float(page.mediabox.height)
        page_fields  = fields_by_page.get(page_num, [])
        is_last_page = (page_num == total_pages)

        if page_fields or (is_last_page and free_signers):
            packet = io.BytesIO()
            c = rl_canvas.Canvas(packet, pagesize=(pw, ph))

            # ── Placed field values ─────────────────────────────────────────
            for f in page_fields:
                val = (f.value or '').strip()
                if not val:
                    continue
                # % coords (top-left origin) → PDF points (bottom-left origin)
                fx  = f.x_pct       / 100.0 * pw
                fw  = f.width_pct   / 100.0 * pw
                fh  = f.height_pct  / 100.0 * ph
                fy  = ph - (f.y_pct / 100.0 * ph) - fh

                if val.startswith('data:image'):
                    try:
                        _, b64 = val.split(',', 1)
                        img_buf = io.BytesIO(base64.b64decode(b64))
                        c.drawImage(ImageReader(img_buf), fx, fy,
                                    width=fw, height=fh,
                                    preserveAspectRatio=True, mask='auto')
                    except Exception as exc:
                        log.warning('Field image error %s: %s', f.id, exc)
                else:
                    font_size = max(6, min(fh * 0.55, 16))
                    c.setFont('Helvetica-Oblique', font_size)
                    c.setFillColorRGB(0.05, 0.10, 0.30)
                    c.drawString(fx + 3, fy + (fh - font_size) / 2.0, val[:80])

                # Thin blue underline — DocuSign style
                c.setStrokeColorRGB(0.18, 0.39, 0.75)
                c.setLineWidth(0.75)
                c.line(fx, fy, fx + fw, fy)

            # ── Free-signing stamps on the last page ────────────────────────
            if is_last_page and free_signers:
                stamp_w = min(200, pw / 3)
                stamp_h = 65
                margin  = 36
                sx = pw - stamp_w - margin
                sy = margin

                for signer in free_signers:
                    sig_data = (signer.signature_data or '').strip()
                    if not sig_data:
                        continue

                    c.setStrokeColorRGB(0.18, 0.39, 0.75)
                    c.setLineWidth(0.6)
                    c.roundRect(sx, sy, stamp_w, stamp_h, 3, fill=0, stroke=1)

                    if sig_data.startswith('data:image'):
                        try:
                            _, b64 = sig_data.split(',', 1)
                            img_buf = io.BytesIO(base64.b64decode(b64))
                            c.drawImage(ImageReader(img_buf),
                                        sx + 4, sy + 18,
                                        width=stamp_w - 8, height=stamp_h - 22,
                                        preserveAspectRatio=True, mask='auto')
                        except Exception as exc:
                            log.warning('Stamp error for %s: %s', signer.name, exc)
                    else:
                        c.setFont('Helvetica-Oblique', 13)
                        c.setFillColorRGB(0.05, 0.10, 0.30)
                        c.drawString(sx + 6, sy + stamp_h / 2 + 2, sig_data[:28])

                    c.setStrokeColorRGB(0.18, 0.39, 0.75)
                    c.setLineWidth(0.5)
                    c.line(sx, sy + 16, sx + stamp_w, sy + 16)

                    c.setFont('Helvetica', 7)
                    c.setFillColorRGB(0.40, 0.40, 0.55)
                    label = signer.name
                    if signer.signed_at:
                        label += '  ' + signer.signed_at.strftime('%d %b %Y')
                    c.drawString(sx + 4, sy + 4, label[:38])

                    sx -= stamp_w + 10
                    if sx < margin:
                        sx  = pw - stamp_w - margin
                        sy += stamp_h + 8

            c.save()
            packet.seek(0)
            page.merge_page(PdfReader(packet).pages[0])

        writer.add_page(page)

    try:
        _add_certificate_page(writer, sig_req)
    except Exception as exc:
        log.warning('Certificate page error: %s', exc)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix='_signed.pdf')
    try:
        with os.fdopen(tmp_fd, 'wb') as tmp_f:
            writer.write(tmp_f)
        base_name = os.path.splitext(os.path.basename(doc.file.name))[0]
        out_name  = base_name + '_signed.pdf'
        if sig_req.signed_document:
            try:
                sig_req.signed_document.delete(save=False)
            except Exception:
                pass
        with open(tmp_path, 'rb') as out_f:
            sig_req.signed_document.save(out_name, _DFile(out_f), save=True)
        log.info('Signed PDF saved for request %s', sig_req.id)
    except Exception as exc:
        log.error('Failed to save signed PDF for request %s: %s', sig_req.id, exc)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


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


class FileDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk, uploaded_by=request.user)
        name, folder_id = f.name, f.folder_id
        f.file.delete(save=False)
        f.delete()
        messages.success(request, f'"{name}" deleted.')
        return redirect(f'/files/?folder={folder_id}' if folder_id else 'file_manager')


class FileShareView(LoginRequiredMixin, View):
    def get(self, request, pk):
        return redirect('file_manager')

    def post(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk, uploaded_by=request.user)
        f.visibility = request.POST.get('visibility', f.visibility)
        f.save(update_fields=['visibility'])
        f.shared_with.clear()
        for uid in request.POST.getlist('shared_with'):
            try:
                from apps.core.models import User
                f.shared_with.add(User.objects.get(id=uid))
            except Exception: pass
        if f.visibility == 'unit':
            uid2 = request.POST.get('unit_id')
            if uid2:
                try:
                    from apps.organization.models import Unit
                    f.unit = Unit.objects.get(id=uid2); f.save(update_fields=['unit'])
                except Exception: pass
        elif f.visibility == 'department':
            did = request.POST.get('dept_id')
            if did:
                try:
                    from apps.organization.models import Department
                    f.department = Department.objects.get(id=did); f.save(update_fields=['department'])
                except Exception: pass
        messages.success(request, f'Sharing updated for "{f.name}".')
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
        folder = get_object_or_404(FileFolder, pk=pk, owner=request.user)
        name, parent_id = folder.name, folder.parent_id
        folder.delete()
        messages.success(request, f'Folder "{name}" deleted.')
        return redirect(f'/files/?folder={parent_id}' if parent_id else 'file_manager')


class FolderShareView(LoginRequiredMixin, View):
    def post(self, request, pk):
        folder = get_object_or_404(FileFolder, pk=pk, owner=request.user)
        vis = request.POST.get('visibility', folder.visibility)
        folder.visibility = vis if vis in {'private','unit','department','office'} else 'private'
        if folder.visibility == 'unit':
            uid = request.POST.get('unit_id')
            if uid:
                try:
                    from apps.organization.models import Unit
                    folder.unit = Unit.objects.get(id=uid)
                except Exception: pass
        elif folder.visibility == 'department':
            did = request.POST.get('dept_id')
            if did:
                try:
                    from apps.organization.models import Department
                    folder.department = Department.objects.get(id=did)
                except Exception: pass
        folder.save()
        messages.success(request, f'Sharing updated for "{folder.name}".')
        next_url = request.POST.get('next', '')
        return redirect(next_url if next_url.startswith('/') else 'file_manager')


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

        # Write to tmp, convert, save back as new SharedFile
        try:
            suffix = '.' + sf.extension
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = tmp.name
                for chunk in sf.file.chunks():
                    tmp.write(chunk)

            pdf_path = _convert_to_pdf(tmp_path)

            pdf_name = os.path.splitext(sf.name)[0] + '.pdf'
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
            # Log to audit if file is part of a sig request
            for sig_req in sf.signature_requests.filter(status__in=['draft','sent','partial']):
                _log_audit(sig_req, 'converted', request=request,
                           notes=f'Converted {sf.name} → {pdf_name}')

            messages.success(request, f'✓ "{sf.name}" converted to PDF — "{pdf_name}" added to your files.')
        except RuntimeError as e:
            messages.error(request, str(e))
        except Exception as e:
            messages.error(request, f'Conversion failed: {e}')
        finally:
            try: os.unlink(tmp_path)
            except Exception: pass
            try: os.unlink(pdf_path)
            except Exception: pass

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
            try:
                suffix = '.' + document.extension
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp_path = tmp.name
                    for chunk in document.file.chunks():
                        tmp.write(chunk)
                pdf_path = _convert_to_pdf(tmp_path)
                pdf_name = os.path.splitext(document.name)[0] + '.pdf'
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
                messages.info(request, f'Document auto-converted to PDF for signing.')
            except RuntimeError as e:
                # LibreOffice not available — proceed with original file
                messages.warning(request, f'Could not auto-convert to PDF ({e}). Proceeding with original file.')
            except Exception as e:
                messages.warning(request, f'Auto-conversion skipped: {e}')
            finally:
                try: os.unlink(tmp_path)
                except Exception: pass
                try: os.unlink(pdf_path)
                except Exception: pass
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
            _notify_signer(signer, base_url)

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
            _notify_signer(signer, base_url)
            messages.success(request, f'Reminder sent to {signer.email}.')
        return redirect('signature_request_detail', pk=sig_req.pk)


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

            if hasattr(sig_req, 'update_status'):
                sig_req.update_status()

            # Write signatures physically into the PDF
            try:
                _embed_signatures_in_pdf(sig_req)
            except Exception as _e:
                import logging as _lg
                _lg.getLogger(__name__).error('PDF embed error for %s: %s', sig_req.id, _e)

            if getattr(sig_req, 'status', None) == 'completed':
                _log_audit(sig_req, 'completed', request=request)
                try:
                    send_mail(
                        subject=f'✓ All signatures collected: {sig_req.title}',
                        message=(
                            f'Your document "{sig_req.title}" has been signed by all parties.\n\n'
                            f'You can view the audit trail in EasyOffice Files.'
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
# AJAX: Convert a file to PDF for the signing annotation canvas
# ─────────────────────────────────────────────────────────────────────────────

class ConvertForSigningView(LoginRequiredMixin, View):
    """
    AJAX POST — converts any supported non-PDF file to PDF so PDF.js can
    render it in the signature annotation canvas (Step 2).
    Returns JSON: {status, file_id, download_url, name}
    The original file is never modified or deleted.
    """
    def post(self, request, pk):
        document = get_object_or_404(SharedFile, pk=pk)

        # Already a PDF — return its details immediately
        if document.is_pdf:
            from django.urls import reverse
            return JsonResponse({
                'status':       'ok',
                'file_id':      str(document.pk),
                'download_url': request.build_absolute_uri(
                    reverse('file_download', kwargs={'pk': document.pk})
                ),
                'name':         document.name,
                'already_pdf':  True,
            })

        if not document.is_convertible:
            return JsonResponse({
                'status':  'error',
                'message': (
                    f'"{document.name}" cannot be converted to PDF. '
                    'Please upload a Word, Excel, PowerPoint, or similar document.'
                ),
            }, status=400)

        tmp_path = pdf_path = None
        try:
            suffix = '.' + document.extension
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = tmp.name
                document.file.open('rb')
                for chunk in document.file.chunks():
                    tmp.write(chunk)
                document.file.close()

            pdf_path = _convert_to_pdf(tmp_path)
            pdf_name = os.path.splitext(document.name)[0] + '.pdf'

            from django.core.files import File as _DFile
            from django.urls import reverse
            with open(pdf_path, 'rb') as pdf_f:
                new_doc = SharedFile.objects.create(
                    name=pdf_name,
                    file=_DFile(pdf_f, name=pdf_name),
                    folder=document.folder,
                    uploaded_by=request.user,
                    visibility=document.visibility,
                    description=f'Auto-converted from "{document.name}" for signing',
                    tags=document.tags,
                    file_size=os.path.getsize(pdf_path),
                    file_type='application/pdf',
                )

            return JsonResponse({
                'status':       'ok',
                'file_id':      str(new_doc.pk),
                'download_url': request.build_absolute_uri(
                    reverse('file_download', kwargs={'pk': new_doc.pk})
                ),
                'name':         pdf_name,
                'message':      f'"{document.name}" converted to PDF successfully.',
            })

        except RuntimeError as exc:
            return JsonResponse({'status': 'error', 'message': str(exc)}, status=500)
        except Exception as exc:
            return JsonResponse({'status': 'error', 'message': f'Conversion failed: {exc}'}, status=500)
        finally:
            for p in (tmp_path, pdf_path):
                try:
                    if p:
                        os.unlink(p)
                except Exception:
                    pass


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

        # Serve the signed copy (with embedded signatures) when available
        if getattr(sig_req, 'signed_document', None) and sig_req.signed_document.name:
            try:
                stem = os.path.splitext(document.name or 'document')[0]
                response = FileResponse(
                    sig_req.signed_document.open('rb'),
                    content_type='application/pdf',
                )
                response['Content-Disposition'] = f'attachment; filename="{stem}_signed.pdf"'
                return response
            except Exception:
                pass  # fall through to original

        # Fallback: original document
        file_name = document.name or os.path.basename(document.file.name)
        content_type, _ = mimetypes.guess_type(file_name)
        content_type = content_type or 'application/octet-stream'
        response = FileResponse(
            document.file.open('rb'),
            content_type=content_type,
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
            if s.sig_type in ('draw', 'type'):
                entry['data'] = s.data
            elif s.image:
                entry['data'] = request.build_absolute_uri(s.image.url)
            sigs.append(entry)
        return JsonResponse({'signatures': sigs})