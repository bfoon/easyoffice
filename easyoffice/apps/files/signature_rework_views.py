# apps/files/signature_rework_views.py
#
# Draft "Rework" workbench for signature requests.
#
# After "Duplicate & Rework" creates a DRAFT SignatureRequest, this module
# gives the creator full control over the draft before it is sent:
#
#   • replace the primary (attached) document
#   • add more documents
#   • remove attached documents
#   • remove signatories (and their placed fields)
#   • add signatories
#   • preview any attached file, and — for PDFs — rotate pages or remove
#     pages IN PLACE while the request is still a draft
#   • send the draft for signature (with ordered / parallel notification,
#     matching the create flow)
#
# Page edits never touch a file that other signature requests reference:
# _ensure_working_copy() silently clones the SharedFile into a fresh
# "(rework)" copy first, so the historical document of the original
# (completed/cancelled) request stays byte-identical for its audit trail.
#
# Wiring:
#   • views.py imports this module at the very bottom (same pattern as
#     external_share_views), so urls.py can keep doing
#     `from apps.files import views` and reference views.SignatureDraftReworkView.
#   • All helpers from views.py are imported lazily inside methods to avoid
#     circular imports, per project convention.

import os
import tempfile
from io import BytesIO

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import View

from pypdf import PdfReader, PdfWriter

from apps.files.models import (
    SharedFile,
    SignatureRequest,
    SignatureRequestSigner,
    SignatureRequestDocument,
    SignatureField,
)


# ─────────────────────────────────────────────────────────────────────────────
# Small local helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_ajax(request):
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'


def _json_err(message, status=400):
    return JsonResponse({'status': 'error', 'message': message}, status=status)


def _attached_rows(sig_req):
    """All SignatureRequestDocument rows for this request, primary first."""
    return list(
        sig_req.documents.select_related('document').order_by('-is_primary', 'order', 'created_at')
    )


def _ensure_primary_row(sig_req):
    """
    Guarantee a primary SignatureRequestDocument row exists for
    sig_req.document (legacy requests may only have the FK).
    """
    row = sig_req.documents.filter(is_primary=True).first()
    if row:
        return row
    row = sig_req.documents.filter(document=sig_req.document).first()
    if row:
        if not row.is_primary:
            row.is_primary = True
            row.save(update_fields=['is_primary'])
        return row
    return SignatureRequestDocument.objects.create(
        request=sig_req, document=sig_req.document, order=1, is_primary=True,
    )


def _ensure_working_copy(sig_req, doc, user):
    """
    Before a destructive in-place page edit, make sure `doc` belongs to
    THIS draft only. If any other SignatureRequest (via the document FK)
    or any other request's SignatureRequestDocument row references the
    same SharedFile, clone the bytes into a fresh SharedFile, repoint the
    draft at the clone, and return (clone, True). Otherwise return
    (doc, False).
    """
    referenced_elsewhere = (
        SignatureRequest.objects.filter(document=doc).exclude(pk=sig_req.pk).exists()
        or SignatureRequestDocument.objects.filter(document=doc).exclude(request=sig_req).exists()
    )
    if not referenced_elsewhere:
        return doc, False

    with doc.file.open('rb') as fh:
        raw = fh.read()

    stem, dot, ext = (doc.name or 'document.pdf').rpartition('.')
    if not dot:
        stem, ext = doc.name, 'pdf'
    new_name = f'{stem} (rework).{ext}'

    clone = SharedFile.objects.create(
        name=new_name,
        uploaded_by=user,
        folder=doc.folder,
        visibility=doc.visibility,
        description=f'Rework copy of {doc.name}',
        tags=doc.tags,
        file_size=len(raw),
        file_type=doc.file_type or 'application/pdf',
    )
    clone.file.save(new_name, ContentFile(raw), save=True)
    try:
        clone.file_hash = clone.compute_hash()
        clone.save(update_fields=['file_hash'])
    except Exception:
        pass

    # Repoint the draft's attachment row …
    row = SignatureRequestDocument.objects.filter(request=sig_req, document=doc).first()
    if row:
        row.document = clone
        row.save(update_fields=['document'])
    # … and the legacy FK if this was the primary.
    if sig_req.document_id == doc.pk:
        sig_req.document = clone
        sig_req.save(update_fields=['document', 'updated_at'])

    return clone, True


def _pdf_page_count(doc):
    try:
        with doc.file.open('rb') as fh:
            return len(PdfReader(fh).pages)
    except Exception:
        return None


def _as_pdf_shared_file(doc, user, request):
    """
    Return a PDF SharedFile for `doc`, auto-converting Office/image files
    the same way SignatureRequestCreateView does. Returns (shared_file,
    converted: bool). Raises RuntimeError with a friendly message on
    failure.
    """
    if getattr(doc, 'is_pdf', False):
        return doc, False
    if not getattr(doc, 'is_convertible', False):
        raise RuntimeError(
            f'"{doc.name}" is not a PDF and cannot be converted for signing.'
        )

    from apps.files.views import _convert_to_pdf, _convert_image_to_pdf  # lazy

    tmp_path = None
    pdf_path = None
    try:
        suffix = '.' + (doc.extension or 'tmp')
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
            for chunk in doc.file.chunks():
                tmp.write(chunk)

        pdf_name = os.path.splitext(doc.name)[0] + '.pdf'
        image_exts = {'jpg', 'jpeg', 'png', 'webp', 'bmp', 'tiff'}

        if (doc.extension or '').lower() in image_exts:
            buf = _convert_image_to_pdf(tmp_path)
            new_doc = SharedFile.objects.create(
                name=pdf_name,
                uploaded_by=user,
                folder=doc.folder,
                visibility=doc.visibility,
                description=f'Auto-converted for signature from {doc.name}',
                tags=doc.tags,
                file_size=buf.getbuffer().nbytes,
                file_type='application/pdf',
            )
            new_doc.file.save(pdf_name, ContentFile(buf.read()), save=True)
        else:
            pdf_path = _convert_to_pdf(tmp_path)
            from django.core.files import File as DjangoFile
            with open(pdf_path, 'rb') as pdf_f:
                new_doc = SharedFile.objects.create(
                    name=pdf_name,
                    file=DjangoFile(pdf_f, name=pdf_name),
                    folder=doc.folder,
                    uploaded_by=user,
                    visibility=doc.visibility,
                    description=f'Auto-converted for signature from {doc.name}',
                    tags=doc.tags,
                    file_size=os.path.getsize(pdf_path),
                    file_type='application/pdf',
                )
        return new_doc, True
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f'Could not convert "{doc.name}" to PDF ({e}).')
    finally:
        for p in (tmp_path, pdf_path):
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
# File picker feed (JSON) for Add / Replace
# ─────────────────────────────────────────────────────────────────────────────

class SignatureDraftFilesView(LoginRequiredMixin, View):
    """
    GET /files/signatures/<pk>/rework/files/

    JSON list of the user's editable files for the Add-document /
    Replace-primary picker on the draft rework screen.
    """

    def get(self, request, pk):
        from apps.files.views import _editable_files_qs  # lazy

        sig_req = get_object_or_404(SignatureRequest, pk=pk)
        if sig_req.created_by_id != request.user.id and not request.user.is_superuser:
            return _json_err('Forbidden', status=403)

        attached_ids = {
            str(r.document_id) for r in sig_req.documents.all()
        }
        attached_ids.add(str(sig_req.document_id))

        files = (
            _editable_files_qs(request.user)
            .filter(is_latest=True)
            .select_related('folder')
            .order_by('-created_at')[:300]
        )

        payload = []
        for f in files:
            payload.append({
                'id': str(f.pk),
                'name': f.name,
                'size': getattr(f, 'size_display', '') or '',
                'is_pdf': bool(getattr(f, 'is_pdf', False)),
                'convertible': bool(getattr(f, 'is_convertible', False)),
                'extension': (f.extension or '').lower(),
                'created': f.created_at.strftime('%b %d, %Y') if f.created_at else '',
                'folder': f.folder.name if f.folder_id else '',
                'attached': str(f.pk) in attached_ids,
            })

        return JsonResponse({'status': 'ok', 'files': payload})


# ─────────────────────────────────────────────────────────────────────────────
# Signer autocomplete feed (JSON)
# ─────────────────────────────────────────────────────────────────────────────

class SignatureSignerUserSearchView(LoginRequiredMixin, View):
    """
    GET /files/signatures/<pk>/rework/users/?q=<term>

    Searches active system users by name or email for the add-signer
    autocomplete on the draft rework screen. Picking a result fills both
    the name and the email; anything not found can still be typed in
    manually — the add_signer action links a User by email when one
    exists and stores a plain external signer otherwise.
    """

    def get(self, request, pk):
        sig_req = get_object_or_404(SignatureRequest, pk=pk)
        if sig_req.created_by_id != request.user.id and not request.user.is_superuser:
            return _json_err('Forbidden', status=403)

        q = (request.GET.get('q') or '').strip()
        if len(q) < 2:
            return JsonResponse({'status': 'ok', 'users': []})

        from apps.core.models import User as CoreUser
        from django.db.models import Q as _Q

        already = set(
            sig_req.signers.values_list('email', flat=True)
        )
        already = {e.lower() for e in already if e}

        users = (
            CoreUser.objects
            .filter(is_active=True, status='active')
            .filter(
                _Q(first_name__icontains=q)
                | _Q(last_name__icontains=q)
                | _Q(email__icontains=q)
            )
            .order_by('first_name', 'last_name')[:8]
        )

        payload = []
        for u in users:
            email = (u.email or '').strip()
            payload.append({
                'id': str(u.pk),
                'name': getattr(u, 'full_name', '') or email,
                'email': email,
                'already_signer': email.lower() in already,
            })

        return JsonResponse({'status': 'ok', 'users': payload})


# ─────────────────────────────────────────────────────────────────────────────
# The rework workbench endpoint
# ─────────────────────────────────────────────────────────────────────────────

class SignatureDraftReworkView(LoginRequiredMixin, View):
    """
    POST /files/signatures/<pk>/rework/

    Draft-only editing endpoint. `action` selects the operation:

        replace_primary   document_id=<uuid>
        add_document      document_id=<uuid>
        remove_document   document_id=<uuid>
        remove_signer     signer_id=<uuid>
        add_signer        name=<str> email=<str>
        rotate_pages      document_id=<uuid> pages=<ranges> degrees=90|180|270
        remove_pages      document_id=<uuid> pages=<ranges>
        send              [expires_days=<int>]

    All actions except `send` are AJAX and return JSON.
    `send` is a normal form POST and redirects back to the detail page.
    """

    # ── entry point ─────────────────────────────────────────────────────────

    def post(self, request, pk):
        sig_req = get_object_or_404(
            SignatureRequest.objects.select_related('document', 'created_by'),
            pk=pk,
        )

        if sig_req.created_by_id != request.user.id and not request.user.is_superuser:
            if _is_ajax(request):
                return _json_err('Forbidden', status=403)
            messages.error(request, 'Only the creator can rework this request.')
            return redirect('signature_request_detail', pk=sig_req.pk)

        if sig_req.status != SignatureRequest.Status.DRAFT:
            msg = 'This request is no longer a draft and cannot be reworked.'
            if _is_ajax(request):
                return _json_err(msg)
            messages.error(request, msg)
            return redirect('signature_request_detail', pk=sig_req.pk)

        action = (request.POST.get('action') or '').strip()
        handler = {
            'replace_primary': self._replace_primary,
            'add_document':    self._add_document,
            'remove_document': self._remove_document,
            'remove_signer':   self._remove_signer,
            'add_signer':      self._add_signer,
            'rotate_pages':    self._rotate_pages,
            'remove_pages':    self._remove_pages,
            'send':            self._send,
        }.get(action)

        if not handler:
            if _is_ajax(request):
                return _json_err('Unknown action')
            messages.error(request, 'Unknown action.')
            return redirect('signature_request_detail', pk=sig_req.pk)

        return handler(request, sig_req)

    # ── document set ────────────────────────────────────────────────────────

    def _resolve_doc(self, request, document_id):
        from apps.files.views import _can_edit_file  # lazy
        if not document_id:
            return None, _json_err('Missing document_id')
        try:
            doc = SharedFile.objects.get(pk=document_id)
        except (SharedFile.DoesNotExist, ValueError):
            return None, _json_err('Document not found', status=404)
        if not _can_edit_file(request.user, doc):
            return None, _json_err('You do not have permission to use this file.', status=403)
        return doc, None

    def _replace_primary(self, request, sig_req):
        from apps.files.views import _log_audit  # lazy

        doc, err = self._resolve_doc(request, request.POST.get('document_id'))
        if err:
            return err

        already = {str(r.document_id) for r in sig_req.documents.exclude(is_primary=True)}
        if str(doc.pk) in already:
            return _json_err('That document is already attached as an extra. Remove it first if you want it as the primary.')

        old_primary = sig_req.document

        try:
            pdf_doc, converted = _as_pdf_shared_file(doc, request.user, request)
        except RuntimeError as e:
            return _json_err(str(e))

        with transaction.atomic():
            row = _ensure_primary_row(sig_req)
            row.document = pdf_doc
            row.save(update_fields=['document'])

            sig_req.document = pdf_doc
            sig_req.save(update_fields=['document', 'updated_at'])

            # Field placements referenced pages of the OLD primary.
            # Drop any field that now points past the end of the new PDF;
            # keep the rest so the creator's layout survives where possible.
            dropped = 0
            new_pages = _pdf_page_count(pdf_doc)
            if new_pages:
                dropped, _ = sig_req.fields.filter(page__gt=new_pages).delete()

            try:
                _log_audit(
                    sig_req, 'edited', request=request,
                    notes=f'Primary document replaced: "{old_primary.name}" → "{pdf_doc.name}"',
                )
            except Exception:
                pass

        note = ''
        if converted:
            note = ' The file was auto-converted to PDF for signing.'
        if dropped:
            note += f' {dropped} field placement(s) beyond the new page count were removed — please review field positions.'

        return JsonResponse({
            'status': 'ok',
            'message': f'Primary document replaced with "{pdf_doc.name}".{note}',
            'document_id': str(pdf_doc.pk),
            'preview_url': reverse('file_preview', kwargs={'pk': pdf_doc.pk}),
        })

    def _add_document(self, request, sig_req):
        from apps.files.views import _log_audit  # lazy

        doc, err = self._resolve_doc(request, request.POST.get('document_id'))
        if err:
            return err

        if SignatureRequestDocument.objects.filter(request=sig_req, document=doc).exists() \
                or sig_req.document_id == doc.pk:
            return _json_err('That document is already attached.')

        order = sig_req.documents.count() + 1
        SignatureRequestDocument.objects.create(
            request=sig_req, document=doc, order=order, is_primary=False,
        )
        try:
            _log_audit(sig_req, 'edited', request=request,
                       notes=f'Document attached: "{doc.name}"')
        except Exception:
            pass

        return JsonResponse({
            'status': 'ok',
            'message': f'"{doc.name}" attached.',
            'document_id': str(doc.pk),
        })

    def _remove_document(self, request, sig_req):
        from apps.files.views import _log_audit  # lazy

        document_id = request.POST.get('document_id')
        row = SignatureRequestDocument.objects.filter(
            request=sig_req, document_id=document_id,
        ).select_related('document').first()
        if not row:
            return _json_err('Not attached', status=404)
        if row.is_primary or str(sig_req.document_id) == str(document_id):
            return _json_err('Cannot remove the primary document — replace it instead.')

        name = row.document.name
        row.delete()
        try:
            _log_audit(sig_req, 'edited', request=request,
                       notes=f'Document removed: "{name}"')
        except Exception:
            pass

        return JsonResponse({'status': 'ok', 'message': f'"{name}" removed.'})

    # ── signers ─────────────────────────────────────────────────────────────

    def _remove_signer(self, request, sig_req):
        from apps.files.views import _log_audit  # lazy

        signer_id = request.POST.get('signer_id')
        signer = sig_req.signers.filter(pk=signer_id).first()
        if not signer:
            return _json_err('Signer not found', status=404)

        if sig_req.signers.count() <= 1:
            return _json_err('A signature request needs at least one signer. Add another signer before removing this one.')

        name, email = signer.name, signer.email

        with transaction.atomic():
            # Their placed fields go with them.
            SignatureField.objects.filter(request=sig_req, signer=signer).delete()
            signer.delete()

            # Renumber the remaining signers 1..n so ordered signing stays sane.
            for i, s in enumerate(
                sig_req.signers.all().order_by('order', 'created_at'), start=1
            ):
                if s.order != i:
                    s.order = i
                    s.save(update_fields=['order'])

            try:
                _log_audit(sig_req, 'edited', request=request,
                           notes=f'Signer removed: {name} <{email}>')
            except Exception:
                pass

        return JsonResponse({'status': 'ok', 'message': f'Signer {name or email} removed.'})

    def _add_signer(self, request, sig_req):
        from apps.files.views import _log_audit  # lazy
        from apps.core.models import User as CoreUser

        name = (request.POST.get('name') or '').strip()
        email = (request.POST.get('email') or '').strip()
        if not email:
            return _json_err('An email address is required.')

        if sig_req.signers.filter(email__iexact=email).exists():
            return _json_err('That email is already on the signer list.')

        user_obj = CoreUser.objects.filter(email__iexact=email).first()
        order = (sig_req.signers.count() or 0) + 1
        signer = SignatureRequestSigner.objects.create(
            request=sig_req,
            user=user_obj,
            email=email,
            name=name or (user_obj.full_name if user_obj else email),
            order=order,
        )
        try:
            _log_audit(sig_req, 'edited', request=request,
                       notes=f'Signer added: {signer.name} <{signer.email}>')
        except Exception:
            pass

        return JsonResponse({
            'status': 'ok',
            'message': f'Signer {signer.name} added.',
            'signer_id': str(signer.pk),
        })

    # ── page tools (in place, on a draft-owned working copy) ────────────────

    def _get_attached_pdf(self, request, sig_req):
        """Resolve document_id → attached PDF, ensuring a safe working copy."""
        document_id = request.POST.get('document_id')
        is_attached = (
            str(sig_req.document_id) == str(document_id)
            or SignatureRequestDocument.objects.filter(
                request=sig_req, document_id=document_id).exists()
        )
        if not is_attached:
            return None, _json_err('That document is not attached to this request.', status=404)

        try:
            doc = SharedFile.objects.get(pk=document_id)
        except (SharedFile.DoesNotExist, ValueError):
            return None, _json_err('Document not found', status=404)

        if not getattr(doc, 'is_pdf', False):
            return None, _json_err('Page tools only work on PDF files.')

        doc, cloned = _ensure_working_copy(sig_req, doc, request.user)
        return (doc, cloned), None

    def _rotate_pages(self, request, sig_req):
        from apps.files.views import (  # lazy
            parse_page_ranges, _replace_shared_pdf_in_place, _log_audit,
        )

        resolved, err = self._get_attached_pdf(request, sig_req)
        if err:
            return err
        doc, cloned = resolved

        pages_raw = (request.POST.get('pages') or '').strip()
        try:
            degrees = int(request.POST.get('degrees', '90'))
        except ValueError:
            degrees = 90
        if degrees not in (90, 180, 270):
            return _json_err('Rotation must be 90, 180, or 270 degrees.')

        try:
            with doc.file.open('rb') as fh:
                reader = PdfReader(fh)
                total = len(reader.pages)
                targets = parse_page_ranges(pages_raw, total) if pages_raw else set(range(1, total + 1))
                if not targets:
                    return _json_err('Please provide valid pages to rotate (e.g. 1,3-5) or leave blank for all pages.')

                writer = PdfWriter()
                for n in range(1, total + 1):
                    page = reader.pages[n - 1]
                    if n in targets:
                        page.rotate(degrees)
                    writer.add_page(page)

            buf = BytesIO()
            writer.write(buf)
            _replace_shared_pdf_in_place(
                doc, buf.getvalue(),
                actor=request.user,
                notes=f'Rotated {degrees}° during signature rework: {pages_raw or "all pages"}',
            )
        except Exception as e:
            return _json_err(f'Could not rotate pages: {e}')

        try:
            _log_audit(sig_req, 'edited', request=request,
                       notes=f'Pages rotated {degrees}° on "{doc.name}" ({pages_raw or "all"})')
        except Exception:
            pass

        msg = f'Rotated {degrees}° on "{doc.name}".'
        if cloned:
            msg += ' A rework copy was created so the original file is untouched.'

        return JsonResponse({
            'status': 'ok',
            'message': msg,
            'document_id': str(doc.pk),
            'page_count': total,
            'preview_url': reverse('file_preview', kwargs={'pk': doc.pk}),
        })

    def _remove_pages(self, request, sig_req):
        from apps.files.views import (  # lazy
            parse_page_ranges, _replace_shared_pdf_in_place, _log_audit,
        )

        resolved, err = self._get_attached_pdf(request, sig_req)
        if err:
            return err
        doc, cloned = resolved

        pages_raw = (request.POST.get('pages') or '').strip()
        if not pages_raw:
            return _json_err('Please provide the pages to remove (e.g. 2 or 4-6).')

        is_primary = (sig_req.document_id == doc.pk)

        try:
            with doc.file.open('rb') as fh:
                reader = PdfReader(fh)
                total = len(reader.pages)
                targets = parse_page_ranges(pages_raw, total)
                if not targets:
                    return _json_err('Please provide valid pages to remove.')
                if len(targets) >= total:
                    return _json_err('You cannot remove every page from the document.')

                writer = PdfWriter()
                for n in range(1, total + 1):
                    if n not in targets:
                        writer.add_page(reader.pages[n - 1])

            buf = BytesIO()
            writer.write(buf)
            new_total = total - len(targets)

            with transaction.atomic():
                _replace_shared_pdf_in_place(
                    doc, buf.getvalue(),
                    actor=request.user,
                    notes=f'Pages removed during signature rework: {pages_raw}',
                )

                dropped_fields = 0
                if is_primary:
                    removed_sorted = sorted(targets)
                    dropped_fields, _ = sig_req.fields.filter(page__in=removed_sorted).delete()
                    # Shift the remaining fields' page numbers down.
                    for field in sig_req.fields.all():
                        shift = sum(1 for r in removed_sorted if r < field.page)
                        if shift:
                            field.page -= shift
                            field.save(update_fields=['page'])

                try:
                    _log_audit(sig_req, 'edited', request=request,
                               notes=f'Pages removed on "{doc.name}": {pages_raw}')
                except Exception:
                    pass
        except Exception as e:
            return _json_err(f'Could not remove pages: {e}')

        msg = f'Removed page(s) {pages_raw} from "{doc.name}" — now {new_total} page(s).'
        if cloned:
            msg += ' A rework copy was created so the original file is untouched.'
        if is_primary and dropped_fields:
            msg += f' {dropped_fields} field placement(s) on the removed pages were deleted.'

        return JsonResponse({
            'status': 'ok',
            'message': msg,
            'document_id': str(doc.pk),
            'page_count': new_total,
            'preview_url': reverse('file_preview', kwargs={'pk': doc.pk}),
        })

    # ── send the draft ──────────────────────────────────────────────────────

    def _send(self, request, sig_req):
        from apps.files.views import (  # lazy
            _notify_signer, _notify, _notify_cc_recipient, _spawn, _log_audit,
        )
        from datetime import timedelta

        signers = list(sig_req.signers.all().order_by('order', 'created_at'))
        if not signers:
            messages.error(request, 'Add at least one signer before sending.')
            return redirect('signature_request_detail', pk=sig_req.pk)
        if not sig_req.document_id:
            messages.error(request, 'Attach a primary document before sending.')
            return redirect('signature_request_detail', pk=sig_req.pk)

        with transaction.atomic():
            sig_req.status = SignatureRequest.Status.SENT
            update_fields = ['status', 'updated_at']
            if request.POST.get('expires_days'):
                try:
                    days = int(request.POST['expires_days'])
                    if days > 0:
                        sig_req.expires_at = timezone.now() + timedelta(days=days)
                        update_fields.append('expires_at')
                except ValueError:
                    pass
            sig_req.save(update_fields=update_fields)

        base_url = request.build_absolute_uri('/')
        is_ordered = bool(getattr(sig_req, 'ordered_signing', False))

        to_notify_now = signers[:1] if is_ordered else signers
        for signer in to_notify_now:
            try:
                if hasattr(signer, 'invited_at'):
                    signer.invited_at = timezone.now()
                    signer.save(update_fields=['invited_at'])
            except Exception:
                pass

            _spawn(_notify_signer, signer, base_url)

            if signer.user:
                _spawn(
                    _notify,
                    signer.user,
                    'sign_request',
                    title=f'Please sign: {sig_req.title}',
                    body=f'Requested by {request.user.full_name}',
                    link=signer.signing_url,
                    sender=request.user,
                    actions=[
                        {'label': 'Sign Now', 'url': signer.signing_url,
                         'style': 'primary', 'icon': 'bi-pen-fill'},
                        {'label': 'View Request',
                         'url': reverse('signature_request_detail', kwargs={'pk': sig_req.pk}),
                         'style': 'secondary', 'icon': 'bi-eye'},
                    ],
                    extra_data={
                        'request_id': str(sig_req.pk),
                        'document_id': str(sig_req.document_id),
                        'signer_id': str(signer.pk),
                    },
                )

        for cc in sig_req.cc_recipients.all():
            try:
                _spawn(_notify_cc_recipient, cc, base_url, event='sent')
            except Exception:
                pass

        _log_audit(sig_req, 'sent', request=request,
                   notes=f'Draft sent — {len(signers)} signer(s), '
                         f'{"ordered" if is_ordered else "parallel"} signing')

        messages.success(
            request,
            f'Signature request "{sig_req.title}" sent to {len(signers)} signer(s).',
        )
        return redirect('signature_request_detail', pk=sig_req.pk)