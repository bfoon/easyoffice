"""
apps/poi/tool_views.py
──────────────────────
Scoped file-tool endpoints for the POI portal. Each one:

  1. resolves the input SharedFile(s),
  2. asserts the POI may access them (scoping.can_access_file),
  3. runs the SAME conversion logic your staff tools use,
  4. writes the OUTPUT into the POI's private workspace folder,
  5. returns the new file id so the portal can show/preview it.

Why wrappers instead of calling your staff views directly: those views assume a
full-access user and would happily operate on files outside a POI's scope. The
wrappers are the scoped front door; the heavy lifting is delegated.

‼️ RECONCILE (delegation): the functions in apps.files that actually perform the
conversions aren't all module-level in the copy I have — your logic lives inside
the view classes (ConvertToPDFView, PDFToWordView, PDFMergeView,
PDFRemovePagesView, SignatureRequestCreateView). Two options:

  (A) EASIEST — extract the conversion bodies into module-level helpers in
      apps/files/services.py (e.g. convert_to_pdf(sf, owner)->SharedFile,
      pdf_to_word(sf, owner)->SharedFile, merge_pdfs(list, owner)->SharedFile,
      remove_pages(sf, pages, owner)->SharedFile) and import them here. This
      keeps ONE implementation shared by staff + POI.

  (B) If you'd rather not refactor now, the _delegate_* functions below fall
      back to calling the staff view's .post() with a synthesised request
      scoped to the POI — works, but couples us to each view's POST contract.

The code below prefers (A) if apps.files.services exists, else uses (B).
"""

import io
import logging

from django.http import JsonResponse, Http404
from django.shortcuts import get_object_or_404
from django.views.generic import View

from apps.poi.guards import POIRequiredMixin
from apps.poi import scoping

log = logging.getLogger(__name__)


def _workspace(request):
    prof = request.poi_profile
    if not prof.workspace_folder_id:
        from apps.poi.admin_views import _ensure_workspace_folder
        _ensure_workspace_folder(prof)
        prof.refresh_from_db()
    return prof.workspace_folder


def _require_file(request, file_id):
    from apps.files.models import SharedFile
    f = get_object_or_404(SharedFile, id=file_id)
    if not scoping.can_access_file(request.user, f):
        raise Http404()
    return f


def _has_services():
    try:
        import apps.files.services  # noqa: F401
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Tool endpoints
# ─────────────────────────────────────────────────────────────────────────────

class POIToConvertPDFView(POIRequiredMixin, View):
    """Convert a doc (docx/image/etc.) to PDF → workspace."""
    def post(self, request, file_id):
        f = _require_file(request, file_id)
        folder = _workspace(request)
        try:
            if _has_services():
                from apps.files.services import convert_to_pdf
                out = convert_to_pdf(f, owner=request.user, folder=folder)
            else:
                out = _delegate_single(request, "file_convert_pdf", f, folder)
        except Exception as e:
            log.exception("POI convert-to-pdf failed")
            return JsonResponse({"ok": False, "error": str(e)}, status=500)
        return _ok_file(out)


class POIPdfToWordView(POIRequiredMixin, View):
    def post(self, request, file_id):
        f = _require_file(request, file_id)
        folder = _workspace(request)
        try:
            if _has_services():
                from apps.files.services import pdf_to_word
                out = pdf_to_word(f, owner=request.user, folder=folder)
            else:
                out = _delegate_single(request, "pdf_to_word", f, folder)
        except Exception as e:
            log.exception("POI pdf-to-word failed")
            return JsonResponse({"ok": False, "error": str(e)}, status=500)
        return _ok_file(out)


class POIRemovePagesView(POIRequiredMixin, View):
    def post(self, request, file_id):
        f = _require_file(request, file_id)
        folder = _workspace(request)
        pages = (request.POST.get("pages") or "").strip()  # e.g. "1,3,5-7"
        try:
            if _has_services():
                from apps.files.services import remove_pages
                out = remove_pages(f, pages, owner=request.user, folder=folder)
            else:
                out = _delegate_single(request, "pdf_remove_pages", f, folder,
                                       extra={"pages": pages})
        except Exception as e:
            log.exception("POI remove-pages failed")
            return JsonResponse({"ok": False, "error": str(e)}, status=500)
        return _ok_file(out)


class POIMergePDFView(POIRequiredMixin, View):
    def post(self, request):
        ids = request.POST.getlist("file_ids") or (
            request.POST.get("file_ids", "").split(",")
        )
        ids = [i.strip() for i in ids if i.strip()]
        files = [_require_file(request, i) for i in ids]
        if len(files) < 2:
            return JsonResponse({"ok": False, "error": "Select at least two PDFs."}, status=400)
        folder = _workspace(request)
        try:
            if _has_services():
                from apps.files.services import merge_pdfs
                out = merge_pdfs(files, owner=request.user, folder=folder)
            else:
                out = _delegate_merge(request, files, folder)
        except Exception as e:
            log.exception("POI merge failed")
            return JsonResponse({"ok": False, "error": str(e)}, status=500)
        return _ok_file(out)


class POISendForSignatureView(POIRequiredMixin, View):
    """Create a signature request from a POI-visible file, addressed to an
    allowed contact (admin/CEO)."""
    def post(self, request, file_id):
        from apps.poi.integrations import get_user_model
        f = _require_file(request, file_id)
        User = get_user_model()
        signer_id = (request.POST.get("signer_id") or "").strip()
        signer = User.objects.filter(id=signer_id).first()
        if not signer or not scoping.is_allowed_contact(signer):
            return JsonResponse(
                {"ok": False, "error": "Signer must be an admin or the CEO."},
                status=403,
            )
        try:
            if _has_services():
                from apps.files.services import create_signature_request
                sig = create_signature_request(
                    document=f, created_by=request.user, signers=[signer],
                )
            else:
                sig = _delegate_signature(request, f, signer)
        except Exception as e:
            log.exception("POI send-for-signature failed")
            return JsonResponse({"ok": False, "error": str(e)}, status=500)
        return JsonResponse({"ok": True, "signature_request_id": str(getattr(sig, "id", ""))})


# ─────────────────────────────────────────────────────────────────────────────
# Delegation fallback (option B) — used only if apps.files.services is absent
# ─────────────────────────────────────────────────────────────────────────────

def _synth_request(request, post_data, files=None):
    """Clone the POI request but with controlled POST so we can drive a staff
    view's .post() while keeping request.user = the POI."""
    new = request
    # We mutate a shallow copy of POST. Django QueryDicts are immutable; rebuild.
    from django.http import QueryDict
    qd = QueryDict("", mutable=True)
    for k, v in post_data.items():
        qd[k] = v
    new.POST = qd
    return new


def _newest_owned_since(user, folder, before_ids):
    from apps.files.models import SharedFile
    qs = SharedFile.objects.filter(uploaded_by=user).order_by("-id")
    for sf in qs[:10]:
        if sf.id not in before_ids:
            if folder and sf.folder_id != folder.id:
                sf.folder = folder
                sf.save(update_fields=["folder"])
            return sf
    return qs.first()


def _delegate_single(request, url_name, sf, folder, extra=None):
    """Drive a staff view that takes <uuid:pk> and produces one output file."""
    from apps.files import views as fviews
    from apps.files.models import SharedFile

    view_map = {
        "file_convert_pdf": fviews.ConvertToPDFView,
        "pdf_to_word": fviews.PDFToWordView,
        "pdf_remove_pages": fviews.PDFRemovePagesView,
    }
    ViewCls = view_map[url_name]
    before = set(SharedFile.objects.filter(uploaded_by=request.user).values_list("id", flat=True))
    post_data = {"doc_id": str(sf.id)}
    if extra:
        post_data.update(extra)
    req = _synth_request(request, post_data)
    # PDFToWord / NotesToPDF read different params; pass pk through kwargs too.
    try:
        ViewCls.as_view()(req, pk=sf.id)
    except TypeError:
        ViewCls.as_view()(req)
    return _newest_owned_since(request.user, folder, before)


def _delegate_merge(request, files, folder):
    from apps.files import views as fviews
    from apps.files.models import SharedFile
    before = set(SharedFile.objects.filter(uploaded_by=request.user).values_list("id", flat=True))
    req = _synth_request(request, {"file_ids": ",".join(str(f.id) for f in files)})
    fviews.PDFMergeView.as_view()(req)
    return _newest_owned_since(request.user, folder, before)


def _delegate_signature(request, sf, signer):
    from apps.files import views as fviews
    from apps.files.models import SignatureRequest
    before = set(SignatureRequest.objects.values_list("id", flat=True))
    req = _synth_request(request, {
        "doc_id": str(sf.id),
        "signers": str(signer.id),
    })
    fviews.SignatureRequestCreateView.as_view()(req)
    return SignatureRequest.objects.exclude(id__in=before).order_by("-id").first()


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ok_file(sf):
    if sf is None:
        return JsonResponse({"ok": False, "error": "No output produced."}, status=500)
    try:
        url = sf.file.url
    except Exception:
        url = ""
    return JsonResponse({
        "ok": True,
        "file": {"id": str(sf.id), "name": sf.name, "url": url},
    })
