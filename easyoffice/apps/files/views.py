import os, subprocess, tempfile, hashlib
import json
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, View
from django.shortcuts import render, redirect, get_object_or_404
from django.http import FileResponse, Http404, JsonResponse, HttpResponse
from django.contrib import messages
from django.utils import timezone
from datetime import timedelta
from django.db.models import Q, Sum, OuterRef, Exists
from django.db import transaction
from django.utils.text import slugify
from django.core.mail import send_mail
from django.conf import settings
import os, subprocess, tempfile, hashlib, mimetypes, re, html, zipfile
from django.utils.decorators import method_decorator
from io import BytesIO
from pathlib import Path
from pypdf import PdfReader, PdfWriter
from django.core.files.base import ContentFile
from pypdf import PdfReader, PdfWriter, Transformation
from PIL import Image
from django.views.decorators.clickjacking import xframe_options_sameorigin
from apps.letterhead.models import LetterheadTemplate
from django.urls import reverse
from django.core.files.storage import default_storage
from apps.files.models import (
    SharedFile,
    FileFolder,
    SignatureRequest,
    FileTrash,
    SignatureRequestSigner,
    SignatureAuditEvent,
    CONVERTIBLE_TYPES,
    FileShareAccess,
    FolderShareAccess,
    FileHistory,
    FilePinnedItem,
    SignatureCC,
    FilePublicToken,
    FileNote, FileNoteShare,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_ajax(request):
    """True when the request was sent via fetch() with X-Requested-With header."""
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def _get_client_ip(request):
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')

def _folder_chain(folder):
    chain = []
    current = folder
    while current:
        chain.append(current)
        current = current.parent
    return chain


def _folder_inherits_from_parent(folder):
    """
    Safe fallback:
    if inherit_parent_sharing field does not exist yet,
    assume inheritance is allowed.
    """
    return getattr(folder, 'inherit_parent_sharing', True)


def _folder_shares_children(folder):
    """
    Safe fallback:
    if share_children field does not exist yet,
    assume parent folder sharing applies to children.
    You can change default to False if you prefer stricter behavior.
    """
    return getattr(folder, 'share_children', True)


def _user_matches_folder_visibility(user, folder):
    if not folder:
        return False

    if folder.owner_id == user.id or user.is_superuser:
        return True

    access = folder.share_access.filter(user=user).exists()
    if access:
        return True

    profile = getattr(user, 'staffprofile', None)
    unit = profile.unit if profile else None
    dept = profile.department if profile else None

    if folder.visibility == 'office':
        return True
    if folder.visibility == 'unit' and folder.unit == unit:
        return True
    if folder.visibility == 'department' and folder.department == dept:
        return True
    if getattr(folder, 'visibility', None) == 'shared_with' and getattr(folder, 'shared_with', None):
        return folder.shared_with.filter(id=user.id).exists()

    return False


def _file_is_visible_via_folder(user, file_obj):
    """
    Checks whether a file becomes visible because its folder
    or one of its ancestor folders is shared with the user.
    """
    if not file_obj.folder:
        return False

    # file can opt out of inherited folder sharing
    if not getattr(file_obj, 'inherit_folder_sharing', True):
        return False

    chain = _folder_chain(file_obj.folder)   # current folder -> parent -> root
    blocked = False

    for index, folder in enumerate(chain):
        if index > 0:
            child_folder = chain[index - 1]
            if not _folder_inherits_from_parent(child_folder):
                blocked = True

        if blocked:
            break

        # current folder always applies directly
        if index == 0 and _user_matches_folder_visibility(user, folder):
            return True

        # ancestor folders only apply if they are allowed to share children
        if index > 0 and _folder_shares_children(folder):
            if _user_matches_folder_visibility(user, folder):
                return True

    return False


def _visible_files_qs(user):
    profile = getattr(user, 'staffprofile', None)
    unit = profile.unit if profile else None
    dept = profile.department if profile else None

    direct_qs = SharedFile.objects.filter(
        Q(uploaded_by=user) |
        Q(visibility='office') |
        Q(visibility='unit', unit=unit) |
        Q(visibility='department', department=dept) |
        Q(shared_with=user) |
        Q(share_access__user=user)
    ).select_related('folder').distinct()

    all_candidate_files = SharedFile.objects.select_related('folder').prefetch_related(
        'share_access',
        'shared_with',
        'folder__share_access',
    ).distinct()

    inherited_ids = [
        f.id for f in all_candidate_files
        if _file_is_visible_via_folder(user, f)
    ]

    if not inherited_ids:
        return direct_qs.distinct()

    return SharedFile.objects.filter(
        Q(id__in=direct_qs.values_list('id', flat=True)) |
        Q(id__in=inherited_ids)
    ).select_related('folder').distinct()

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

    direct_perm = None
    if f.visibility == 'office':
        direct_perm = 'view'
    elif f.visibility == 'unit' and f.unit == unit:
        direct_perm = 'view'
    elif f.visibility == 'department' and f.department == dept:
        direct_perm = 'view'
    elif f.shared_with.filter(id=user.id).exists():
        direct_perm = 'view'

    inherited_perm = None
    if getattr(f, 'inherit_folder_sharing', True) and f.folder_id:
        inherited_perm = _inherited_folder_permission_for(user, f.folder)

    return _max_perm(direct_perm, inherited_perm)


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


def _editable_files_qs(user):
    """
    Files the user can edit: their own uploads PLUS any file where they have
    'edit' or 'full' access via an explicit FileShareAccess row.
    Used by tool pages (PDF tools, Quick Sign, Convert) so shared-edit users
    see files they can act on.
    """
    return SharedFile.objects.filter(
        Q(uploaded_by=user) |
        Q(share_access__user=user, share_access__permission__in=('edit', 'full'))
    ).distinct()


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

def _replace_shared_pdf_in_place(pdf, raw_bytes, actor=None, notes=''):
    """
    Overwrite the binary content of an existing SharedFile in place.
    Used by live preview PDF editing so the same file updates immediately.
    """
    storage_name = (pdf.file.name or '').strip()
    if not storage_name:
        storage_name = f'shared_files/{timezone.now():%Y/%m}/{pdf.name}'

    try:
        if default_storage.exists(storage_name):
            default_storage.delete(storage_name)
    except Exception:
        pass

    saved_name = default_storage.save(storage_name, ContentFile(raw_bytes))
    pdf.file.name = saved_name
    pdf.file_size = len(raw_bytes)
    pdf.file_type = 'application/pdf'
    pdf.file_hash = hashlib.sha256(raw_bytes).hexdigest()
    pdf.version = (pdf.version or 1) + 1
    pdf.save(update_fields=['file', 'file_size', 'file_type', 'file_hash', 'version', 'updated_at'])

    try:
        _log_file_history(
            pdf,
            FileHistory.Action.UPDATED,
            actor=actor,
            notes=notes or 'PDF updated in preview'
        )
    except Exception:
        pass

    return pdf

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
    """Send a professional HTML signing invitation email to a signer."""
    from django.core.mail import EmailMessage
    sign_url = base_url.rstrip('/') + signer.signing_url
    org_name = getattr(settings, 'ORGANISATION_NAME',
                       getattr(settings, 'OFFICE_NAME', 'EasyOffice'))
    org_email = getattr(settings, 'DEFAULT_FROM_EMAIL', f'noreply@{org_name.lower().replace(" ","")}.org')
    requester = signer.request.created_by.full_name
    title     = signer.request.title
    message   = signer.request.message or ''
    expires   = (signer.request.expires_at.strftime('%d %B %Y')
                 if signer.request.expires_at else None)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
  body{{margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;}}
  .w{{max-width:600px;margin:28px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);}}
  .hdr{{background:linear-gradient(135deg,#1e3a5f,#7c3aed);padding:36px 40px;text-align:center;}}
  .hdr h1{{margin:0 0 4px;font-size:22px;color:#fff;font-weight:800;}}
  .hdr p{{margin:0;font-size:13px;color:rgba(255,255,255,.75);}}
  .pen{{font-size:40px;display:block;margin-bottom:12px;}}
  .body{{padding:32px 40px;}}
  .greeting{{font-size:16px;color:#1e293b;line-height:1.7;margin-bottom:20px;}}
  .doc-box{{background:#f8fafc;border:1.5px solid #e2e8f0;border-radius:12px;padding:16px 20px;margin:20px 0;display:flex;align-items:center;gap:14px;}}
  .doc-box i{{font-size:2rem;color:#ef4444;flex-shrink:0;}}
  .doc-name{{font-weight:700;font-size:15px;color:#1e293b;}}
  .doc-from{{font-size:13px;color:#64748b;margin-top:3px;}}
  .msg-box{{background:#fffbeb;border:1px solid #fde68a;border-radius:10px;padding:14px 18px;font-size:14px;color:#78350f;margin:16px 0;line-height:1.6;font-style:italic;}}
  .cta{{text-align:center;margin:28px 0;}}
  .btn{{display:inline-block;background:linear-gradient(135deg,#7c3aed,#4f46e5);color:#fff!important;padding:14px 36px;border-radius:12px;text-decoration:none;font-weight:800;font-size:16px;letter-spacing:-.2px;box-shadow:0 4px 16px rgba(124,58,237,.35);}}
  .url-note{{font-size:12px;color:#94a3b8;text-align:center;margin-top:8px;word-break:break-all;}}
  .warn{{background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:10px 14px;font-size:13px;color:#991b1b;margin-top:20px;}}
  .expire{{background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:10px 14px;font-size:13px;color:#9a3412;margin-top:10px;}}
  .footer{{background:#f8fafc;padding:20px 40px;text-align:center;border-top:1px solid #e2e8f0;}}
  .footer p{{margin:0;font-size:12px;color:#94a3b8;line-height:1.8;}}
</style></head>
<body>
<div class="w">
  <div class="hdr">
    <span class="pen">✍️</span>
    <h1>Signature Required</h1>
    <p>{org_name} — Document Signing Request</p>
  </div>
  <div class="body">
    <p class="greeting">Dear <strong>{signer.name}</strong>,<br><br>
    <strong>{requester}</strong> has requested your digital signature on the following document.</p>

    <div class="doc-box">
      <div style="font-size:2rem;color:#ef4444">📄</div>
      <div>
        <div class="doc-name">{title}</div>
        <div class="doc-from">From {requester} · {org_name}</div>
      </div>
    </div>

    {'<div class="msg-box">💬 ' + message + '</div>' if message else ''}

    <div class="cta">
      <a href="{sign_url}" class="btn">🖊 Review &amp; Sign Document</a>
      <div class="url-note">Or copy this link: {sign_url}</div>
    </div>

    {'<div class="expire">⏰ This request expires on <strong>' + expires + '</strong>. Please sign before then.</div>' if expires else ''}

    <div class="warn">🔒 This signing link is <strong>unique to you</strong> and should not be shared with anyone else. No login is required — clicking the link will take you directly to the document.</div>
  </div>
  <div class="footer">
    <p><strong>{org_name}</strong><br>This is an automated signing request. Please do not reply to this email.</p>
  </div>
</div>
</body></html>"""

    msg = EmailMessage(
        subject=f'Action Required: Please sign "{title}" | {org_name}',
        body=html,
        from_email=org_email,
        to=[signer.email],
    )
    msg.content_subtype = 'html'
    # Attach the document so signer can review it in their email client too
    try:
        doc = signer.request.document
        doc.file.open('rb')
        msg.attach(doc.name, doc.file.read(), 'application/octet-stream')
        doc.file.close()
    except Exception:
        pass
    try:
        msg.send()
    except Exception:
        pass


def _notify_cc_recipient(cc, base_url, event='sent'):
    """Notify CC/viewer recipients when a signature request is sent or completed."""
    from django.core.mail import EmailMessage
    org_name  = getattr(settings, 'ORGANISATION_NAME',
                        getattr(settings, 'OFFICE_NAME', 'EasyOffice'))
    org_email = getattr(settings, 'DEFAULT_FROM_EMAIL', f'noreply@{org_name.lower().replace(" ","")}.org')
    req       = cc.request
    view_url  = base_url.rstrip('/') + cc.view_url if cc.role == 'viewer' else ''

    if event == 'sent':
        subject_line = f'You are CC\'d on: "{req.title}" | {org_name}'
        body_intro   = f'You have been copied on a document signing request sent by <strong>{req.created_by.full_name}</strong>.'
        action_block = (
            f'<div style="text-align:center;margin:24px 0"><a href="{view_url}" style="display:inline-block;background:#3b82f6;color:#fff;padding:12px 28px;border-radius:10px;text-decoration:none;font-weight:700">View Signing Progress</a></div>'
            if view_url else ''
        )
    else:  # completed
        subject_line = f'Signing Complete: "{req.title}" | {org_name}'
        body_intro   = f'All signers have completed signing <strong>"{req.title}"</strong>. The signed document is attached below.'
        action_block = (
            f'<div style="text-align:center;margin:24px 0"><a href="{view_url}" style="display:inline-block;background:#10b981;color:#fff;padding:12px 28px;border-radius:10px;text-decoration:none;font-weight:700">View Signed Document</a></div>'
            if view_url else ''
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/>
<style>
  body{{margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;}}
  .w{{max-width:600px;margin:28px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);}}
  .hdr{{background:linear-gradient(135deg,#1e3a5f,#3b82f6);padding:32px 40px;text-align:center;}}
  .hdr h1{{margin:0;font-size:20px;color:#fff;font-weight:800;}}
  .hdr p{{margin:6px 0 0;font-size:13px;color:rgba(255,255,255,.75);}}
  .body{{padding:28px 40px;font-size:15px;color:#1e293b;line-height:1.7;}}
  .footer{{background:#f8fafc;padding:18px 40px;text-align:center;border-top:1px solid #e2e8f0;font-size:12px;color:#94a3b8;}}
</style></head>
<body>
<div class="w">
  <div class="hdr">
    <h1>{'📋 Document Signing Update' if event == 'sent' else '✅ Signing Complete'}</h1>
    <p>{org_name}</p>
  </div>
  <div class="body">
    <p>Dear <strong>{cc.name}</strong>,</p>
    <p>{body_intro}</p>
    <p><strong>Document:</strong> {req.title}<br>
    <strong>Requested by:</strong> {req.created_by.full_name}</p>
    {action_block}
  </div>
  <div class="footer">{org_name} · Document Signing System. This is an automated notification.</div>
</div>
</body></html>"""

    msg = EmailMessage(subject=subject_line, body=html, from_email=org_email, to=[cc.email])
    msg.content_subtype = 'html'
    # Attach signed doc if completed
    if event == 'completed':
        try:
            doc = req.document
            doc.file.open('rb')
            msg.attach(doc.name, doc.file.read(), 'application/octet-stream')
            doc.file.close()
        except Exception:
            pass
    try:
        msg.send()
    except Exception:
        pass


def _send_completion_email(sig_req, base_url):
    """
    Send a professional completion email to the creator and all signers
    with the signed document attached and a public no-login download link.
    """
    from django.core.mail import EmailMessage
    from datetime import timedelta
    org_name  = getattr(settings, 'ORGANISATION_NAME',
                        getattr(settings, 'OFFICE_NAME', 'EasyOffice'))
    org_email = getattr(settings, 'DEFAULT_FROM_EMAIL', f'noreply@{org_name.lower().replace(" ","")}.org')

    # Create a public download token valid for 30 days
    token_obj = FilePublicToken.objects.create(
        file       = sig_req.document,
        label      = f'Signed copy — {sig_req.title}',
        created_by = sig_req.created_by,
        expires_at = timezone.now() + timedelta(days=30),
    )
    public_url = base_url.rstrip('/') + token_obj.public_url

    recipients = [sig_req.created_by.email] if sig_req.created_by.email else []
    for signer in sig_req.signers.filter(status='signed'):
        if signer.email and signer.email not in recipients:
            recipients.append(signer.email)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"/>
<style>
  body{{margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;}}
  .w{{max-width:600px;margin:28px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);}}
  .hdr{{background:linear-gradient(135deg,#064e3b,#10b981);padding:36px 40px;text-align:center;}}
  .hdr h1{{margin:0;font-size:22px;color:#fff;font-weight:800;}}
  .hdr p{{margin:6px 0 0;font-size:13px;color:rgba(255,255,255,.8);}}
  .check{{font-size:48px;display:block;margin-bottom:10px;}}
  .body{{padding:32px 40px;}}
  .summary{{background:#ecfdf5;border:1.5px solid #6ee7b7;border-radius:12px;padding:16px 20px;margin:20px 0;}}
  .summary h3{{margin:0 0 10px;font-size:15px;font-weight:700;color:#065f46;}}
  .signer-row{{display:flex;align-items:center;gap:10px;padding:6px 0;font-size:14px;color:#064e3b;}}
  .cta{{text-align:center;margin:28px 0;}}
  .btn{{display:inline-block;background:linear-gradient(135deg,#10b981,#059669);color:#fff!important;padding:13px 32px;border-radius:12px;text-decoration:none;font-weight:800;font-size:15px;}}
  .expire-note{{font-size:12px;color:#6b7280;text-align:center;margin-top:8px;}}
  .attach-note{{background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:10px 14px;font-size:13px;color:#065f46;margin-top:16px;}}
  .footer{{background:#f8fafc;padding:18px 40px;text-align:center;border-top:1px solid #e2e8f0;font-size:12px;color:#94a3b8;}}
</style></head>
<body>
<div class="w">
  <div class="hdr">
    <span class="check">✅</span>
    <h1>Document Fully Signed</h1>
    <p>{org_name} · Signing Complete</p>
  </div>
  <div class="body">
    <p style="font-size:16px;color:#1e293b;line-height:1.7">
      All parties have signed <strong>"{sig_req.title}"</strong>.
      The signed document is attached to this email and can also be downloaded via the link below.
    </p>

    <div class="summary">
      <h3>✍ Signatories</h3>
      {''.join(f'<div class="signer-row">✓ <strong>{s.name}</strong> &lt;{s.email}&gt; — signed {s.signed_at.strftime("%d %b %Y at %H:%M") if s.signed_at else ""}</div>' for s in sig_req.signers.filter(status="signed"))}
    </div>

    <div class="cta">
      <a href="{public_url}" class="btn">⬇️ Download Signed Document</a>
      <div class="expire-note">This download link expires in 30 days. No login required.</div>
    </div>

    <div class="attach-note">📎 The signed document is also attached directly to this email for your records.</div>
  </div>
  <div class="footer">{org_name} · Document Signing System<br>This is an automated notification. Reference: {sig_req.id}</div>
</div>
</body></html>"""

    for recipient_email in recipients:
        msg = EmailMessage(
            subject=f'✅ Signing Complete: "{sig_req.title}" | {org_name}',
            body=html,
            from_email=org_email,
            to=[recipient_email],
        )
        msg.content_subtype = 'html'
        # Attach the document
        try:
            sig_req.document.file.open('rb')
            msg.attach(sig_req.document.name, sig_req.document.file.read(), 'application/octet-stream')
            sig_req.document.file.close()
        except Exception:
            pass
        try:
            msg.send()
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

# views.py

def _perm_rank(perm):
    return {'view': 1, 'edit': 2, 'full': 3}.get(perm or '', 0)


def _max_perm(*perms):
    best = None
    for p in perms:
        if _perm_rank(p) > _perm_rank(best):
            best = p
    return best


def _inherited_folder_permission_for(user, folder):
    """
    Returns permission inherited from this folder or its ancestors.
    Respects:
      - share_children on ancestor folder
      - inherit_parent_sharing on intermediate subfolders
    """
    if not folder:
        return None

    chain = _folder_chain(folder)  # current -> parent -> root
    best = None
    blocked = False

    for idx, current in enumerate(chain):
        if idx > 0:
            child = chain[idx - 1]
            if hasattr(child, 'inherit_parent_sharing') and not child.inherit_parent_sharing:
                blocked = True
        if blocked:
            break

        perm = _folder_permission_for(user, current)

        # direct folder itself always applies
        if idx == 0 and perm:
            best = _max_perm(best, perm)
            continue

        # ancestors only apply if share_children=True
        if perm and getattr(current, 'share_children', False):
            best = _max_perm(best, perm)

    return best

def _zip_preview(self, f):
    import zipfile
    from datetime import datetime

    try:
        f.file.open('rb')
        z = zipfile.ZipFile(f.file)

        file_list = []

        for info in z.infolist():
            file_list.append({
                'name': info.filename,
                'size': info.file_size,
                'compressed_size': info.compress_size,
                'is_dir': info.is_dir(),
                'modified': datetime(*info.date_time),
            })

        z.close()
        f.file.close()

        return render(self.request, 'files/preview_zip.html', {
            'file': f,
            'zip_files': file_list
        })

    except zipfile.BadZipFile:
        return render(self.request, 'files/preview_error.html', {
            'error': 'Invalid or corrupted ZIP file.'
        })

def _safe_zip_member_name(member_name):
    """
    Normalize a zip member name and block dangerous paths.
    Returns a safe relative path string or None if unsafe.
    """
    if not member_name:
        return None

    name = member_name.replace('\\', '/').strip()

    # remove leading slashes
    while name.startswith('/'):
        name = name[1:]

    if not name:
        return None

    parts = []
    for part in name.split('/'):
        part = part.strip()
        if not part or part == '.':
            continue
        if part == '..':
            return None
        parts.append(part)

    if not parts:
        return None

    return '/'.join(parts)


def _guess_content_type(filename):
    content_type, _ = mimetypes.guess_type(filename)
    return content_type or 'application/octet-stream'


def _unique_file_name_for_folder(folder, original_name):
    root, ext = os.path.splitext(original_name)
    candidate = original_name
    counter = 2

    while SharedFile.objects.filter(folder=folder, name=candidate, is_latest=True).exists():
        candidate = f"{root} ({counter}){ext}"
        counter += 1

    return candidate


def _ensure_pdf_shared_file(source_file, user):
    if source_file.is_pdf:
        return source_file

    if not source_file.is_convertible:
        raise RuntimeError("Selected document cannot be converted to PDF.")

    if not getattr(source_file, 'file', None):
        raise RuntimeError("Selected document has no file attached.")

    source_name = (
        source_file.name
        or os.path.basename(getattr(source_file.file, 'name', '') or '')
        or f'document-{source_file.pk}'
    )

    tmp_path = None
    pdf_path = None

    try:
        suffix = f".{source_file.extension}" if source_file.extension else ""
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
            for chunk in source_file.file.chunks():
                tmp.write(chunk)

        pdf_name = os.path.splitext(source_name)[0] + ".pdf"
        image_exts = {'jpg', 'jpeg', 'png', 'webp', 'bmp', 'tiff'}

        if source_file.extension.lower() in image_exts:
            pdf_buffer = _convert_image_to_pdf(tmp_path)
            new_pdf = SharedFile.objects.create(
                name=_unique_file_name_for_folder(source_file.folder, pdf_name),
                uploaded_by=user,
                folder=source_file.folder,
                visibility=source_file.visibility,
                description=f'Auto-converted from {source_name}',
                tags=source_file.tags,
                file_size=pdf_buffer.getbuffer().nbytes,
                file_type='application/pdf',
            )
            new_pdf.file.save(pdf_name, ContentFile(pdf_buffer.read()), save=True)
            return new_pdf

        pdf_path = _convert_to_pdf(tmp_path)
        with open(pdf_path, 'rb') as pdf_f:
            from django.core.files import File as DjangoFile

            new_pdf = SharedFile.objects.create(
                name=_unique_file_name_for_folder(source_file.folder, pdf_name),
                file=DjangoFile(pdf_f, name=pdf_name),
                folder=source_file.folder,
                uploaded_by=user,
                visibility=source_file.visibility,
                description=f'Auto-converted from {source_name}',
                tags=source_file.tags,
                file_size=os.path.getsize(pdf_path),
                file_type='application/pdf',
            )
            return new_pdf

    finally:
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass
        try:
            if pdf_path and os.path.exists(pdf_path):
                os.unlink(pdf_path)
        except Exception:
            pass


def _apply_pdf_letterhead(
    source_pdf_path,
    letterhead_pdf_path,
    apply_mode='first',
    x_pct=10.0,
    y_pct=18.0,
    width_pct=80.0,
    height_pct=70.0,
):
    src_reader = PdfReader(source_pdf_path)
    letter_reader = PdfReader(letterhead_pdf_path)
    writer = PdfWriter()

    if not letter_reader.pages:
        raise RuntimeError("Letterhead PDF is empty.")

    letterhead_base = letter_reader.pages[0]

    bg_w = float(letterhead_base.mediabox.width)
    bg_h = float(letterhead_base.mediabox.height)

    x_pct = max(0.0, min(100.0, float(x_pct)))
    y_pct = max(0.0, min(100.0, float(y_pct)))
    width_pct = max(5.0, min(100.0, float(width_pct)))
    height_pct = max(5.0, min(100.0, float(height_pct)))

    box_x = bg_w * (x_pct / 100.0)
    box_w = bg_w * (width_pct / 100.0)
    box_h = bg_h * (height_pct / 100.0)

    for index, src_page in enumerate(src_reader.pages):
        use_letterhead = (apply_mode == 'all') or (apply_mode == 'first' and index == 0)

        if use_letterhead:
            writer.add_page(letterhead_base)
            out_page = writer.pages[-1]

            if getattr(out_page, "rotation", 0):
                out_page.transfer_rotation_to_content()

            src_w = float(src_page.mediabox.width)
            src_h = float(src_page.mediabox.height)

            scale = min(box_w / src_w, box_h / src_h)

            placed_w = src_w * scale
            placed_h = src_h * scale

            placed_x = box_x + ((box_w - placed_w) / 2.0)

            top_y = bg_h * (y_pct / 100.0)
            box_bottom = bg_h - top_y - box_h
            placed_y = box_bottom + ((box_h - placed_h) / 2.0)

            transformation = (
                Transformation()
                .scale(scale, scale)
                .translate(placed_x, placed_y)
            )

            out_page.merge_transformed_page(
                src_page,
                transformation,
                over=True,
                expand=False,
            )
        else:
            writer.add_page(src_page)

    output = BytesIO()
    writer.write(output)
    output.seek(0)
    return output
# ─────────────────────────────────────────────────────────────────────────────
# File Manager
# ─────────────────────────────────────────────────────────────────────────────

class FileManagerView(LoginRequiredMixin, TemplateView):
    template_name = 'files/file_manager.html'

    def get(self, request, *args, **kwargs):
        ctx = self.get_context_data(**kwargs)

        if (request.GET.get('partial') == '1'
                and request.headers.get('X-Requested-With') == 'XMLHttpRequest'):
            # Partial render — try dedicated partial template first,
            # then fall back to rendering the grid section inline.
            # This works even if _file_grid.html has not been copied to
            # the templates directory yet.
            from django.template.loader import render_to_string
            from django.template.exceptions import TemplateDoesNotExist
            from django.http import HttpResponse

            for tmpl_name in ['files/_file_grid.html', 'files/file_manager_grid.html']:
                try:
                    html = render_to_string(tmpl_name, ctx, request=request)
                    return HttpResponse(html)
                except TemplateDoesNotExist:
                    continue
                except Exception:
                    break  # unexpected error — fall through to full page

            # Last resort: render the full template but extract the #fileGrid
            # section so the JS doesn't detect DOCTYPE and hard-reload.
            # We signal "no partial available" with a custom header so the JS
            # can reload properly if needed.
            response = self.render_to_response(ctx)
            response['X-Partial-Unavailable'] = '1'
            return response

        return self.render_to_response(ctx)

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
            'name':           'name',
            '-name':          '-name',
            'created_at':     'created_at',
            '-created_at':    '-created_at',
            'file_size':      'file_size',
            '-file_size':     '-file_size',
            'download_count': 'download_count',
            '-download_count':'-download_count',
        }
        sort_field = allowed_sorts.get(sort, '-created_at')
        files = files.order_by(sort_field)

        # Sort folders by the same column where possible
        folder_sort_map = {
            'name': 'name', '-name': '-name',
            'created_at': 'created_at', '-created_at': '-created_at',
        }
        folders = folders.order_by(folder_sort_map.get(sort, 'name'))

        # ── Annotate each file/folder with the requesting user's permission ──
        # This lets the template show/hide buttons based on 'view'/'edit'/'full'
        # without needing custom template filters.
        _OFFICE_EDITABLE = {'docx', 'xlsx', 'pptx'}

        # ── Note badge annotations (has_my_note / has_shared_note) ───────────
        # Must be applied BEFORE list() so the Exists subqueries are evaluated
        # in the same SQL query and the template variables are populated.
        files = _annotate_note_badges(files, user)

        file_list = list(files)
        for f in file_list:
            f.user_permission = _file_permission_for(user, f)
            # Build share_access_data: {user_id: permission} for existing shares
            f.share_access_data = {
                str(sa.user_id): sa.permission
                for sa in f.share_access.select_related('user').all()
            }
            f.shared_user_ids = list(f.share_access_data.keys())
            # Flag for collaborative editor button
            f.is_office_editable = (
                getattr(f, 'extension', None) or ''
            ).lower().strip('.') in _OFFICE_EDITABLE

        folder_list = list(folders)
        for folder in folder_list:
            folder.user_permission = _folder_permission_for(user, folder)
            folder.share_access_data = {
                str(sa.user_id): sa.permission
                for sa in folder.share_access.select_related('user').all()
            }
            folder.shared_user_ids = list(folder.share_access_data.keys())

        my_qs          = _visible_files_qs(user).filter(uploaded_by=user)
        my_total_size  = my_qs.aggregate(s=Sum('file_size'))['s'] or 0

        # ── Pinned items ──────────────────────────────────────────────────────
        pinned_qs = FilePinnedItem.objects.filter(user=user).select_related('file','folder')
        pinned_file_ids   = {str(p.file_id)   for p in pinned_qs if p.file_id}
        pinned_folder_ids = {str(p.folder_id) for p in pinned_qs if p.folder_id}

        for f in file_list:
            f.is_pinned = str(f.id) in pinned_file_ids
        for folder in folder_list:
            folder.is_pinned = str(folder.id) in pinned_folder_ids

        # Re-sort: pinned items float to top, but within pinned AND within
        # unpinned groups the user's chosen sort order is preserved.
        # Python's sort is stable so we only need one key: (not is_pinned, original_index)
        file_list   = sorted(enumerate(file_list),   key=lambda t: (not t[1].is_pinned, t[0]))
        file_list   = [f for _, f in file_list]
        folder_list = sorted(enumerate(folder_list), key=lambda t: (not t[1].is_pinned, t[0]))
        folder_list = [f for _, f in folder_list]

        # Pending signatures count for badge
        pending_sigs   = SignatureRequestSigner.objects.filter(
            user=user, status='pending'
        ).count()

        from apps.core.models import User as CoreUser
        from apps.organization.models import Unit, Department
        ctx.update({
            'files': file_list, 'folders': folder_list,
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
            try:
                folder = FileFolder.objects.get(id=folder_id)
            except FileFolder.DoesNotExist:
                folder = None

        project = None
        project_id = request.POST.get('project_id')
        if project_id:
            try:
                from apps.projects.models import Project
                project = Project.objects.get(id=project_id)
            except Project.DoesNotExist:
                project = None

        original_name = os.path.basename(f.name or '').strip()
        typed_name = request.POST.get('name', '').strip()

        # Preserve extension from uploaded file
        original_root, original_ext = os.path.splitext(original_name)
        original_ext = original_ext or ''

        if typed_name:
            typed_root, typed_ext = os.path.splitext(typed_name)
            if typed_ext:
                final_name = typed_name
            else:
                final_name = f'{typed_name}{original_ext}'
        else:
            final_name = original_name or f'upload{original_ext or ""}'

        sf = SharedFile.objects.create(
            name=final_name,
            file=f,
            folder=folder,
            project=project,
            uploaded_by=request.user,
            visibility=request.POST.get('visibility', 'private'),
            description=request.POST.get('description', ''),
            tags=request.POST.get('tags', ''),
            file_size=f.size,
            file_type=f.content_type or '',
        )

        try:
            sf.file_hash = sf.compute_hash()
            sf.save(update_fields=['file_hash'])
        except Exception:
            pass

        messages.success(request, f'"{sf.name}" uploaded successfully.')

        if _is_ajax(request):
            return JsonResponse({
                'ok': True,
                'message': f'"{sf.name}" uploaded successfully.',
                'file': {
                    'id': str(sf.pk),
                    'name': sf.name,
                    'folder_id': str(sf.folder_id) if sf.folder_id else '',
                },
            })

        next_url = request.POST.get('next', '')
        if next_url and next_url.startswith('/'):
            return redirect(next_url)

        if project:
            return redirect('project_detail', pk=project.id)

        if folder:
            return redirect(f'/files/?folder={folder.id}')

        return redirect('file_manager')


class FileDownloadView(LoginRequiredMixin, View):
    def get(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)

        if not _file_permission_for(request.user, f):
            raise Http404

        f.download_count = (f.download_count or 0) + 1
        f.save(update_fields=['download_count'])

        try:
            _log_file_history(f, 'downloaded', actor=request.user, notes='Downloaded file')
        except Exception:
            pass

        return FileResponse(f.file.open('rb'), as_attachment=True, filename=f.name)

@method_decorator(xframe_options_sameorigin, name='dispatch')
class FilePreviewView(LoginRequiredMixin, View):
    """
    Serve a file inline for the preview modal.

    - Office documents are converted to PDF and cached.
    - Text files are served as plain text.
    - ZIP files are previewed as an HTML listing using zipfile.
    - Everything else is served inline as-is.
    """

    _OFFICE_EXTS = {
        'doc', 'docx', 'odt', 'rtf',
        'xls', 'xlsx', 'ods',
        'ppt', 'pptx', 'odp',
    }

    _TEXT_EXTS = {
        'md', 'py', 'js', 'html', 'css', 'sql', 'xml', 'json', 'csv', 'txt',
        'yml', 'yaml', 'ini', 'log'
    }

    _ZIP_EXTS = {'zip'}

    def get(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)

        if not _file_permission_for(request.user, f):
            raise Http404

        ext = (f.extension or '').lower().strip('.')

        if ext in self._ZIP_EXTS:
            return self._zip_preview(f)

        if ext in self._OFFICE_EXTS:
            return self._office_preview(f, ext)

        if ext in self._TEXT_EXTS:
            response = FileResponse(
                f.file.open('rb'),
                content_type='text/plain; charset=utf-8'
            )
            response['Content-Disposition'] = f'inline; filename="{f.name}"'
            response['X-Frame-Options'] = 'SAMEORIGIN'
            return response

        content_type, _ = mimetypes.guess_type(f.name)
        content_type = content_type or 'application/octet-stream'
        response = FileResponse(f.file.open('rb'), content_type=content_type)
        response['Content-Disposition'] = f'inline; filename="{f.name}"'
        response['X-Frame-Options'] = 'SAMEORIGIN'
        return response

    def _zip_preview(self, f):
        try:
            with f.file.open('rb') as fh:
                with zipfile.ZipFile(fh) as zf:
                    infos = zf.infolist()

                    total_files = 0
                    total_dirs = 0
                    total_uncompressed = 0
                    total_compressed = 0
                    rows = []

                    for info in infos:
                        is_dir = info.is_dir()
                        if is_dir:
                            total_dirs += 1
                            kind = 'Folder'
                            size_display = '—'
                            compressed_display = '—'
                        else:
                            total_files += 1
                            total_uncompressed += info.file_size
                            total_compressed += info.compress_size
                            kind = 'File'
                            size_display = self._human_size(info.file_size)
                            compressed_display = self._human_size(info.compress_size)

                        rows.append(
                            f"""
                            <tr>
                                <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;white-space:nowrap;">{kind}</td>
                                <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;">{html.escape(info.filename)}</td>
                                <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;white-space:nowrap;text-align:right;">{size_display}</td>
                                <td style="padding:8px 10px;border-bottom:1px solid #e5e7eb;white-space:nowrap;text-align:right;">{compressed_display}</td>
                            </tr>
                            """
                        )

        except zipfile.BadZipFile:
            return HttpResponse(
                'ZIP preview unavailable: invalid or corrupted ZIP archive.',
                content_type='text/plain',
                status=422,
            )
        except Exception as exc:
            return HttpResponse(
                f'ZIP preview unavailable: {exc}',
                content_type='text/plain',
                status=422,
            )

        archive_size = self._human_size(getattr(f, 'file_size', 0) or 0)
        total_uncompressed_display = self._human_size(total_uncompressed)
        total_compressed_display = self._human_size(total_compressed)

        body_rows = ''.join(rows) or """
            <tr>
                <td colspan="4" style="padding:14px;text-align:center;color:#6b7280;">
                    Archive is empty.
                </td>
            </tr>
        """

        html_content = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <title>{html.escape(f.name)} - ZIP Preview</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    margin: 0;
                    padding: 0;
                    background: #f9fafb;
                    color: #111827;
                }}
                .wrap {{
                    max-width: 1100px;
                    margin: 0 auto;
                    padding: 20px;
                }}
                .card {{
                    background: #fff;
                    border: 1px solid #e5e7eb;
                    border-radius: 12px;
                    overflow: hidden;
                    box-shadow: 0 4px 14px rgba(0,0,0,0.04);
                }}
                .head {{
                    padding: 18px 20px;
                    border-bottom: 1px solid #e5e7eb;
                    background: #f3f4f6;
                }}
                .title {{
                    margin: 0;
                    font-size: 18px;
                    font-weight: 700;
                    word-break: break-word;
                }}
                .meta {{
                    margin-top: 8px;
                    display: flex;
                    flex-wrap: wrap;
                    gap: 10px;
                    font-size: 13px;
                    color: #4b5563;
                }}
                .badge {{
                    display: inline-block;
                    padding: 4px 8px;
                    border-radius: 999px;
                    background: #eef2ff;
                    color: #4338ca;
                    font-weight: 600;
                }}
                .table-wrap {{
                    overflow: auto;
                }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    font-size: 14px;
                }}
                thead th {{
                    text-align: left;
                    padding: 10px;
                    background: #f9fafb;
                    border-bottom: 1px solid #e5e7eb;
                    position: sticky;
                    top: 0;
                }}
                .muted {{
                    color: #6b7280;
                }}
            </style>
        </head>
        <body>
            <div class="wrap">
                <div class="card">
                    <div class="head">
                        <h1 class="title">ZIP Preview: {html.escape(f.name)}</h1>
                        <div class="meta">
                            <span class="badge">{total_files} file{"s" if total_files != 1 else ""}</span>
                            <span class="badge">{total_dirs} folder{"s" if total_dirs != 1 else ""}</span>
                            <span>Archive size: <strong>{archive_size}</strong></span>
                            <span>Total content size: <strong>{total_uncompressed_display}</strong></span>
                            <span>Total compressed size: <strong>{total_compressed_display}</strong></span>
                        </div>
                    </div>

                    <div class="table-wrap">
                        <table>
                            <thead>
                                <tr>
                                    <th style="width:110px;">Type</th>
                                    <th>Name</th>
                                    <th style="width:140px;text-align:right;">Size</th>
                                    <th style="width:170px;text-align:right;">Compressed</th>
                                </tr>
                            </thead>
                            <tbody>
                                {body_rows}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </body>
        </html>
        """

        response = HttpResponse(html_content, content_type='text/html; charset=utf-8')
        response['Content-Disposition'] = f'inline; filename="{f.name}.zip-preview.html"'
        response['X-Frame-Options'] = 'SAMEORIGIN'
        return response

    def _office_preview(self, f, ext):
        import shutil

        cache_dir = Path(tempfile.gettempdir()) / 'eo_preview_cache'
        cache_dir.mkdir(exist_ok=True)

        cache_key = f'{f.pk}_{getattr(f, "file_hash", None) or "v1"}.pdf'
        cache_path = cache_dir / cache_key

        if not cache_path.exists():
            src_tmp = tempfile.NamedTemporaryFile(
                suffix=f'.{ext}',
                delete=False,
                dir=tempfile.gettempdir()
            )
            try:
                with f.file.open('rb') as original:
                    src_tmp.write(original.read())
                src_tmp.flush()
                src_tmp.close()

                pdf_path = _convert_to_pdf(src_tmp.name)
                shutil.copy2(pdf_path, str(cache_path))
            except Exception as exc:
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

    def _human_size(self, size):
        try:
            size = int(size or 0)
        except (TypeError, ValueError):
            return '0 B'

        units = ['B', 'KB', 'MB', 'GB', 'TB']
        value = float(size)
        unit = 0

        while value >= 1024 and unit < len(units) - 1:
            value /= 1024.0
            unit += 1

        if unit == 0:
            return f'{int(value)} {units[unit]}'
        return f'{value:.1f} {units[unit]}'

from django.urls import reverse

class FilePreviewInfoView(LoginRequiredMixin, View):
    def get(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)

        if not _file_permission_for(request.user, f):
            return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)

        data = {
            'ok': True,
            'id': str(f.pk),
            'name': f.name,
            'is_pdf': bool(getattr(f, 'is_pdf', False)),
            'can_edit': bool(_can_edit_file(request.user, f)),
            'preview_url': reverse('file_preview', kwargs={'pk': f.pk}),
        }

        if data['is_pdf']:
            try:
                with f.file.open('rb') as fh:
                    page_count = len(PdfReader(fh).pages)
            except Exception as exc:
                return JsonResponse({'ok': False, 'error': f'Could not read PDF: {exc}'}, status=422)

            data.update({
                'page_count': page_count,
                'remove_url': reverse('pdf_remove_pages', kwargs={'pk': f.pk}),
                'reorder_url': reverse('pdf_reorder_pages', kwargs={'pk': f.pk}),
            })

        return JsonResponse(data)

class PDFRemovePagesView(LoginRequiredMixin, View):
    def post(self, request, pk):
        pdf = get_object_or_404(SharedFile, pk=pk)

        if not _can_edit_file(request.user, pdf):
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': 'You do not have permission to edit this file.'}, status=403)
            messages.error(request, 'You do not have permission to edit this file.')
            return redirect('pdf_tools_page')

        if not getattr(pdf, 'is_pdf', False):
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': 'This file is not a PDF.'}, status=400)
            messages.error(request, 'This file is not a PDF.')
            return redirect('pdf_tools_page')

        remove_pages_raw = request.POST.get('remove_pages', '').strip()
        overwrite = (request.POST.get('overwrite') or '').strip().lower() in {'1', 'true', 'yes'}
        output_name = request.POST.get('output_name', '').strip() or f'{pdf.name.rsplit(".", 1)[0]}-edited.pdf'

        try:
            with pdf.file.open('rb') as fh:
                reader = PdfReader(fh)
                writer = PdfWriter()

                total_pages = len(reader.pages)
                remove_pages = parse_page_ranges(remove_pages_raw, total_pages)

                if not remove_pages:
                    if _is_ajax(request):
                        return JsonResponse({'ok': False, 'error': 'Please provide valid pages to remove.'}, status=400)
                    messages.error(request, 'Please provide valid pages to remove.')
                    return redirect('pdf_tools_page')

                if len(remove_pages) >= total_pages:
                    if _is_ajax(request):
                        return JsonResponse({'ok': False, 'error': 'You cannot remove every page from the document.'}, status=400)
                    messages.error(request, 'You cannot remove every page from the document.')
                    return redirect('pdf_tools_page')

                for page_num in range(1, total_pages + 1):
                    if page_num not in remove_pages:
                        writer.add_page(reader.pages[page_num - 1])

            buffer = BytesIO()
            writer.write(buffer)
            raw_bytes = buffer.getvalue()

            if overwrite:
                storage_name = (pdf.file.name or '').strip()
                if storage_name:
                    try:
                        from django.core.files.storage import default_storage
                        if default_storage.exists(storage_name):
                            default_storage.delete(storage_name)
                    except Exception:
                        pass

                pdf.file.save(pdf.name, ContentFile(raw_bytes), save=False)
                pdf.file_size = len(raw_bytes)
                pdf.file_type = 'application/pdf'
                pdf.file_hash = hashlib.sha256(raw_bytes).hexdigest()
                pdf.version = (pdf.version or 1) + 1
                pdf.save(update_fields=['file', 'file_size', 'file_type', 'file_hash', 'version', 'updated_at'])

                try:
                    _log_file_history(
                        pdf,
                        FileHistory.Action.UPDATED,
                        actor=request.user,
                        notes=f'Pages removed in preview: {remove_pages_raw}'
                    )
                except Exception:
                    pass

                return JsonResponse({
                    'ok': True,
                    'message': f'"{pdf.name}" updated successfully.',
                    'file_id': str(pdf.pk),
                    'page_count': total_pages - len(remove_pages),
                })

            if not output_name.lower().endswith('.pdf'):
                output_name += '.pdf'

            new_file = SharedFile.objects.create(
                name=output_name,
                uploaded_by=request.user,
                visibility=pdf.visibility,
                folder=pdf.folder,
                description=f'Edited from {pdf.name}',
                tags=pdf.tags,
                file_size=len(raw_bytes),
                file_type='application/pdf',
            )
            new_file.file.save(output_name, ContentFile(raw_bytes), save=True)

            try:
                new_file.file_hash = new_file.compute_hash()
                new_file.save(update_fields=['file_hash'])
            except Exception:
                pass

            if _is_ajax(request):
                return JsonResponse({
                    'ok': True,
                    'message': f'Edited PDF created: "{output_name}".',
                    'file_id': str(new_file.pk),
                })

            messages.success(request, f'Edited PDF created: "{output_name}".')
        except Exception as e:
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': f'Could not remove pages: {e}'}, status=400)
            messages.error(request, f'Could not remove pages: {e}')

        return redirect('pdf_tools_page')

class FileDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)

        if not _can_delete_file(request.user, f):
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)
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

        if f.file:
            f.file.open('rb')
            trash.file_blob.save(
                os.path.basename(f.file.name),
                ContentFile(f.file.read()),
                save=True
            )

        try:
            _log_file_history(f, 'deleted', actor=request.user, notes='Moved to recycle bin')
        except Exception:
            pass

        f.delete()

        messages.success(request, f'"{name}" moved to recycle bin.')

        if _is_ajax(request):
            return JsonResponse({'ok': True, 'message': f'"{name}" moved to recycle bin.', 'file_id': str(pk)})

        return redirect(f'/files/?folder={folder_id}' if folder_id else 'file_manager')

class FileShareView(LoginRequiredMixin, View):
    def get(self, request, pk):
        return redirect('file_manager')

    def post(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)
        if not _can_edit_file(request.user, f):
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)
            messages.error(request, 'You do not have permission to change sharing on this file.')
            return redirect('file_manager')

        vis = (request.POST.get('visibility') or f.visibility or 'private').strip().lower()
        allowed = {'private', 'shared_with', 'unit', 'department', 'office'}
        if vis not in allowed:
            vis = 'private'

        # Track previous direct shares so only newly-added users are notified
        previous_shared_ids = set(f.shared_with.values_list('id', flat=True))

        # Reset old sharing state
        f.visibility = vis
        f.unit = None
        f.department = None
        f.inherit_folder_sharing = request.POST.get('inherit_folder_sharing', '1') == '1'
        f.save(update_fields=['visibility', 'unit', 'department', 'inherit_folder_sharing'])

        # Clear previous direct shares + permission rows
        f.shared_with.clear()
        f.share_access.all().delete()

        added_recipients = []

        if vis == 'shared_with':
            from apps.core.models import User

            selected_user_ids = request.POST.getlist('shared_with')

            for uid in selected_user_ids:
                try:
                    recipient = User.objects.get(id=uid, is_active=True)
                except User.DoesNotExist:
                    continue

                if recipient == request.user:
                    continue

                perm = (request.POST.get(f'perm_{uid}', 'view') or 'view').strip().lower()
                if perm not in ('view', 'edit', 'full'):
                    perm = 'view'

                # Keep legacy M2M so existing visibility logic still works if used elsewhere
                f.shared_with.add(recipient)

                FileShareAccess.objects.update_or_create(
                    file=f,
                    user=recipient,
                    defaults={
                        'permission': perm,
                        'granted_by': request.user,
                    }
                )

                if recipient.id not in previous_shared_ids:
                    added_recipients.append((recipient, perm))

            # Do not leave a broken shared_with state
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
                except Unit.DoesNotExist:
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
                except Department.DoesNotExist:
                    f.visibility = 'private'
                    f.save(update_fields=['visibility'])
            else:
                f.visibility = 'private'
                f.save(update_fields=['visibility'])

        # office visibility needs no extra target object

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

        if _is_ajax(request):
            return JsonResponse({
                'ok': True,
                'message': f'Sharing updated for "{f.name}".' if f.visibility != 'private' else f'Sharing stopped for "{f.name}".',
                'visibility': f.visibility,
            })

        return redirect(next_url if next_url.startswith('/') else 'file_manager')


class FolderCreateView(LoginRequiredMixin, View):
    def post(self, request):
        name = request.POST.get('name', '').strip()
        if not name:
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': 'Folder name required.'}, status=400)
            messages.error(request, 'Folder name required.')
            return redirect('file_manager')
        parent_id = request.POST.get('parent_id')
        parent = None
        if parent_id:
            try: parent = FileFolder.objects.get(id=parent_id, owner=request.user)
            except FileFolder.DoesNotExist: pass
        folder = FileFolder.objects.create(
            name=name, owner=request.user, parent=parent,
            visibility=request.POST.get('visibility', 'private'),
            color=request.POST.get('color', '#f59e0b'),
        )
        messages.success(request, f'Folder "{name}" created.')

        if _is_ajax(request):
            return JsonResponse({
                'ok': True,
                'message': f'Folder "{name}" created.',
                'folder': {'id': str(folder.pk), 'name': folder.name},
            })

        next_url = request.POST.get('next', '')
        return redirect(next_url if next_url.startswith('/') else 'file_manager')


class FolderMoveView(LoginRequiredMixin, View):
    def post(self, request, pk):
        folder = get_object_or_404(FileFolder, pk=pk)

        if not _can_edit_folder(request.user, folder):
            return JsonResponse({
                'status': 'error',
                'message': 'You do not have permission to move this folder.',
            }, status=403)

        parent_id = (request.POST.get('parent_id') or '').strip()
        new_parent = None

        if parent_id:
            new_parent = get_object_or_404(FileFolder, pk=parent_id)

            if not _can_edit_folder(request.user, new_parent):
                return JsonResponse({
                    'status': 'error',
                    'message': 'You do not have permission to move into that folder.',
                }, status=403)

            # prevent moving into itself
            if str(new_parent.id) == str(folder.id):
                return JsonResponse({
                    'status': 'error',
                    'message': 'A folder cannot be moved into itself.',
                }, status=400)

            # prevent moving into descendant
            current = new_parent
            while current:
                if str(current.id) == str(folder.id):
                    return JsonResponse({
                        'status': 'error',
                        'message': 'Cannot move a folder into one of its subfolders.',
                    }, status=400)
                current = current.parent

        folder.parent = new_parent
        folder.save(update_fields=['parent'])

        return JsonResponse({
            'status': 'ok',
            'message': 'Folder moved successfully.',
            'folder_id': str(folder.id),
            'parent_id': str(new_parent.id) if new_parent else None,
        })

class FolderDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        folder = get_object_or_404(FileFolder, pk=pk)

        if not _can_delete_folder(request.user, folder):
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)
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

        if _is_ajax(request):
            return JsonResponse({'ok': True, 'message': f'Folder "{name}" moved to recycle bin.', 'folder_id': str(pk)})

        return redirect(f'/files/?folder={parent_id}' if parent_id else 'file_manager')

class PermanentDeleteTrashFileView(LoginRequiredMixin, View):
    def post(self, request, pk):
        item = get_object_or_404(
            FileTrash,
            pk=pk,
            owner=request.user,
            is_restored=False,
            item_type='file',
        )

        item_name = item.name

        # delete stored trash blob from storage if present
        try:
            if item.file_blob:
                item.file_blob.delete(save=False)
        except Exception:
            pass

        item.delete()

        if _is_ajax(request):
            return JsonResponse({
                'ok': True,
                'message': f'"{item_name}" permanently deleted.',
                'trash_id': pk,
            })

        messages.success(request, f'"{item_name}" permanently deleted.')
        return redirect('recycle_bin')

class FolderShareView(LoginRequiredMixin, View):
    def post(self, request, pk):
        folder = get_object_or_404(FileFolder, pk=pk)
        if not _can_edit_folder(request.user, folder):
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)
            messages.error(request, 'You do not have permission to change sharing on this folder.')
            return redirect('file_manager')

        previous_visibility = folder.visibility
        previous_unit_id = folder.unit_id
        previous_department_id = folder.department_id
        previous_shared_ids = set(folder.share_access.values_list('user_id', flat=True))

        vis = (request.POST.get('visibility') or folder.visibility or 'private').strip().lower()
        allowed = {'private', 'shared_with', 'unit', 'department', 'office'}
        if vis not in allowed:
            vis = 'private'

        folder.visibility = vis
        folder.unit = None
        folder.department = None

        folder.share_children = request.POST.get('share_children') == '1'
        folder.inherit_parent_sharing = request.POST.get('inherit_parent_sharing', '1') == '1'

        # Clear old direct-share permissions first
        folder.share_access.all().delete()

        added_recipients = []

        if vis == 'shared_with':
            from apps.core.models import User

            selected_user_ids = request.POST.getlist('shared_with')

            for uid in selected_user_ids:
                try:
                    recipient = User.objects.get(id=uid, is_active=True)
                except User.DoesNotExist:
                    continue

                if recipient == request.user:
                    continue

                perm = (request.POST.get(f'perm_{uid}', 'view') or 'view').strip().lower()
                if perm not in ('view', 'edit', 'full'):
                    perm = 'view'

                FolderShareAccess.objects.update_or_create(
                    folder=folder,
                    user=recipient,
                    defaults={
                        'permission': perm,
                        'granted_by': request.user,
                    }
                )

                if recipient.id not in previous_shared_ids:
                    added_recipients.append((recipient, perm))

            if not folder.share_access.exists():
                folder.visibility = 'private'

        elif vis == 'unit':
            uid = (request.POST.get('unit_id') or '').strip()
            if uid:
                try:
                    from apps.organization.models import Unit
                    folder.unit = Unit.objects.get(id=uid)
                except Unit.DoesNotExist:
                    folder.visibility = 'private'
            else:
                folder.visibility = 'private'

        elif vis == 'department':
            did = (request.POST.get('dept_id') or '').strip()
            if did:
                try:
                    from apps.organization.models import Department
                    folder.department = Department.objects.get(id=did)
                except Department.DoesNotExist:
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

        next_url = request.POST.get('next', '')

        if _is_ajax(request):
            return JsonResponse({
                'ok': True,
                'message': f'Sharing updated for "{folder.name}".' if folder.visibility != 'private' else f'Sharing stopped for "{folder.name}".',
                'visibility': folder.visibility,
            })

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
        sf = get_object_or_404(SharedFile, pk=pk)
        if not _can_edit_file(request.user, sf):
            messages.error(request, 'You do not have permission to convert this file.')
            return redirect('file_manager')

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
            document = get_object_or_404(SharedFile, pk=pk)
            if not _can_edit_file(request.user, document):
                messages.error(request, 'You do not have permission to send this file for signature.')
                return redirect('file_manager')
        ctx = {
            'document': document,
            'my_files': _editable_files_qs(request.user).order_by('-created_at')[:50],
            'all_staff': __import__('apps.core.models', fromlist=['User']).User.objects.filter(
                is_active=True, status='active'
            ).exclude(id=request.user.id).order_by('first_name'),
        }
        return render(request, self.template_name, ctx)

    def post(self, request, pk=None):
        doc_id = request.POST.get('document_id') or (str(pk) if pk else None)
        document = get_object_or_404(SharedFile, pk=doc_id)

        if not _can_edit_file(request.user, document):
            messages.error(request, 'You do not have permission to send this file for signature.')
            return redirect('file_manager')

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

        # ── Save CC / viewer recipients ───────────────────────────────────────
        cc_names  = request.POST.getlist('cc_name')
        cc_emails = request.POST.getlist('cc_email')
        cc_roles  = request.POST.getlist('cc_role')
        for i, email in enumerate(cc_emails):
            email = email.strip()
            if not email:
                continue
            name  = (cc_names[i].strip()  if i < len(cc_names)  else '') or email
            role  = (cc_roles[i].strip()  if i < len(cc_roles)  else '') or 'cc'
            from apps.core.models import User as CoreUser
            internal_user = CoreUser.objects.filter(email__iexact=email).first()
            cc_obj = SignatureCC.objects.create(
                request=sig_req,
                user=internal_user,
                email=email,
                name=name,
                role=role,
            )
            _notify_cc_recipient(cc_obj, base_url, event='sent')

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
                    _send_completion_email(sig_req, request.build_absolute_uri('/'))
                except Exception:
                    pass
                # Notify CC recipients of completion
                try:
                    for cc in sig_req.cc_recipients.all():
                        _notify_cc_recipient(cc, request.build_absolute_uri('/'), event='completed')
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
                    from django.core.mail import EmailMessage as _EM
                    _org = getattr(settings,'ORGANISATION_NAME',getattr(settings,'OFFICE_NAME','EasyOffice'))
                    _from = getattr(settings,'DEFAULT_FROM_EMAIL',f'noreply@{_org.lower().replace(" ","")}.org')
                    _reason_html = f'<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:12px 16px;font-size:14px;color:#991b1b;margin:14px 0"><strong>Reason:</strong> {signer.decline_reason}</div>' if signer.decline_reason else ''
                    _detail_url = request.build_absolute_uri(f'/files/signatures/{sig_req.pk}/')
                    _html = f'''<!DOCTYPE html><html><head><meta charset="UTF-8"/><style>body{{margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;}} .w{{max-width:580px;margin:28px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);}} .hdr{{background:linear-gradient(135deg,#7f1d1d,#ef4444);padding:32px 36px;text-align:center;}} .hdr h1{{margin:0;color:#fff;font-size:20px;font-weight:800;}} .hdr p{{margin:6px 0 0;color:rgba(255,255,255,.75);font-size:13px;}} .body{{padding:28px 36px;font-size:15px;color:#1e293b;line-height:1.7;}} .btn{{display:inline-block;background:#3b82f6;color:#fff;padding:11px 26px;border-radius:10px;text-decoration:none;font-weight:700;}} .footer{{background:#f8fafc;padding:16px 36px;border-top:1px solid #e2e8f0;text-align:center;font-size:12px;color:#94a3b8;}}</style></head><body><div class="w"><div class="hdr"><h1>❌ Signing Declined</h1><p>{_org}</p></div><div class="body"><p>Dear <strong>{sig_req.created_by.full_name}</strong>,</p><p><strong>{signer.name}</strong> ({signer.email}) has <strong>declined</strong> to sign <em>"{sig_req.title}"</em>.</p>{_reason_html}<div style="text-align:center;margin:20px 0"><a href="{_detail_url}" class="btn">View Signature Request →</a></div></div><div class="footer">{_org} · Document Signing System</div></div></body></html>'''
                    _m = _EM(subject=f'Declined: {signer.name} declined to sign "{sig_req.title}"',body=_html,from_email=_from,to=[sig_req.created_by.email])
                    _m.content_subtype='html'
                    _m.send()
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
        all_editable = list(_editable_files_qs(user).order_by('-created_at'))
        ctx['convertible_files'] = [f for f in all_editable if f.is_convertible]
        ctx['pdf_files']         = [f for f in all_editable if f.is_pdf]
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

        action = (request.POST.get('action') or '').strip()

        def _json_ok(message, extra=None):
            payload = {'status': 'ok', 'message': message}
            if extra:
                payload.update(extra)
            return JsonResponse(payload)

        def _json_err(message, status=400):
            return JsonResponse({'status': 'error', 'message': message}, status=status)

        wants_json = (
            request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or request.headers.get('Accept', '').lower().find('application/json') >= 0
        )

        if action == 'save':
            sig_type = request.POST.get('sig_type', 'draw').strip()
            sig_name = request.POST.get('name', 'My Signature').strip() or 'My Signature'
            is_default = str(request.POST.get('is_default', '')).lower() in ('1', 'true', 'yes', 'on')

            sig = SavedSignature(
                user=request.user,
                name=sig_name,
                sig_type=sig_type,
                is_default=is_default,
            )

            if sig_type == 'draw':
                sig.data = request.POST.get('data', '').strip()
                if not sig.data:
                    return _json_err('No drawn signature data provided.') if wants_json else redirect('saved_signatures')

            elif sig_type == 'type':
                sig.data = request.POST.get('data', '').strip()
                if not sig.data:
                    return _json_err('No typed signature data provided.') if wants_json else redirect('saved_signatures')

            elif sig_type == 'upload':
                if 'image' not in request.FILES:
                    return _json_err('No uploaded signature image provided.') if wants_json else redirect('saved_signatures')
                sig.image = request.FILES['image']

            else:
                return _json_err('Invalid signature type.') if wants_json else redirect('saved_signatures')

            sig.save()

            message = f'Signature "{sig.name}" saved.'
            if wants_json:
                payload = {
                    'id': str(sig.id),
                    'name': sig.name,
                    'type': sig.sig_type,
                    'is_default': sig.is_default,
                    'data': sig.data if sig.sig_type in ('draw', 'type') else request.build_absolute_uri(sig.image.url),
                }
                return _json_ok(message, payload)

            messages.success(request, message)
            return redirect('saved_signatures')

        elif action == 'delete':
            sig_id = request.POST.get('sig_id')
            sig = get_object_or_404(SavedSignature, id=sig_id, user=request.user)
            sig.delete()

            if wants_json:
                return _json_ok('Signature deleted.', {'sig_id': sig_id})

            messages.success(request, 'Signature deleted.')
            return redirect('saved_signatures')

        elif action == 'set_default':
            sig_id = request.POST.get('sig_id')
            sig = get_object_or_404(SavedSignature, id=sig_id, user=request.user)
            sig.is_default = True
            sig.save()

            if wants_json:
                return _json_ok(f'"{sig.name}" set as default.', {'sig_id': str(sig.id)})

            messages.success(request, f'"{sig.name}" set as default.')
            return redirect('saved_signatures')

        if wants_json:
            return _json_err('Invalid action.')

        messages.error(request, 'Invalid action.')
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
                'id': str(s.id),
                'name': s.name,
                'type': s.sig_type,
                'is_default': s.is_default,
            }

            if s.sig_type == 'draw':
                entry['data'] = s.data

            elif s.sig_type == 'type':
                raw = s.data or ''
                if raw.startswith('font:') and '|' in raw:
                    font_part, text_part = raw.split('|', 1)
                    entry['font'] = font_part[5:]
                    entry['text'] = text_part
                else:
                    entry['font'] = 'Dancing Script'
                    entry['text'] = raw
                entry['data'] = raw

            elif s.image:
                entry['data'] = request.build_absolute_uri(s.image.url)
            else:
                entry['data'] = s.data or ''

            sigs.append(entry)

        return JsonResponse({'signatures': sigs})


class FileMoveView(LoginRequiredMixin, View):
    def post(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)

        if not _can_edit_file(request.user, f):
            return JsonResponse({
                'status': 'error',
                'message': 'You do not have permission to move this file.',
            }, status=403)

        folder_id = (request.POST.get('folder_id') or '').strip()
        folder = None

        if folder_id:
            folder = get_object_or_404(FileFolder, id=folder_id)

            if not _can_edit_folder(request.user, folder):
                return JsonResponse({
                    'status': 'error',
                    'message': 'You do not have permission to move files into that folder.',
                }, status=403)

        old_folder_name = f.folder.name if f.folder else 'My Drive'
        new_folder_name = folder.name if folder else 'My Drive'

        f.folder = folder
        f.save(update_fields=['folder'])

        try:
            _log_file_history(
                f,
                'moved',
                actor=request.user,
                notes=f'Moved from "{old_folder_name}" to "{new_folder_name}"'
            )
        except Exception:
            pass

        return JsonResponse({
            'status': 'ok',
            'message': f'"{f.name}" moved successfully.',
            'folder_id': str(folder.id) if folder else '',
            'folder_name': new_folder_name,
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

class PDFSplitView(LoginRequiredMixin, View):
    def post(self, request, pk):
        pdf = get_object_or_404(SharedFile, pk=pk)
        if not _can_edit_file(request.user, pdf):
            messages.error(request, 'You do not have permission to edit this file.')
            return redirect('pdf_tools_page')

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
        pdf = get_object_or_404(SharedFile, pk=pk)
        if not _can_edit_file(request.user, pdf):
            messages.error(request, 'You do not have permission to edit this file.')
            return redirect('pdf_tools_page')

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
        pdf = get_object_or_404(SharedFile, pk=pk)

        if not _can_edit_file(request.user, pdf):
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': 'You do not have permission to edit this file.'}, status=403)
            messages.error(request, 'You do not have permission to edit this file.')
            return redirect('pdf_tools_page')

        if not getattr(pdf, 'is_pdf', False):
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': 'This file is not a PDF.'}, status=400)
            messages.error(request, 'This file is not a PDF.')
            return redirect('pdf_tools_page')

        page_order_raw = request.POST.get('page_order', '').strip()
        overwrite = (request.POST.get('overwrite') or '').strip().lower() in {'1', 'true', 'yes'}

        try:
            with pdf.file.open('rb') as fh:
                reader = PdfReader(fh)
                total_pages = len(reader.pages)

                page_order = [int(x.strip()) for x in page_order_raw.split(',') if x.strip()]
                if sorted(page_order) != list(range(1, total_pages + 1)):
                    if _is_ajax(request):
                        return JsonResponse({'ok': False, 'error': 'Page order must include every page exactly once.'}, status=400)
                    messages.error(request, 'Page order must include every page exactly once.')
                    return redirect('pdf_tools_page')

                writer = PdfWriter()
                for page_num in page_order:
                    writer.add_page(reader.pages[page_num - 1])

            buffer = BytesIO()
            writer.write(buffer)
            raw_bytes = buffer.getvalue()

            if overwrite:
                _replace_shared_pdf_in_place(
                    pdf,
                    raw_bytes,
                    actor=request.user,
                    notes=f'Pages reordered in preview: {page_order_raw}'
                )

                return JsonResponse({
                    'ok': True,
                    'message': f'"{pdf.name}" reordered successfully.',
                    'file_id': str(pdf.pk),
                    'page_count': total_pages,
                    'preview_url': reverse('file_preview', kwargs={'pk': pdf.pk}),
                })

            output_name = f'{pdf.name.rsplit(".", 1)[0]}-reordered.pdf'
            new_file = SharedFile.objects.create(
                name=output_name,
                uploaded_by=request.user,
                visibility=pdf.visibility,
                folder=pdf.folder,
                description=f'Reordered from {pdf.name}',
                tags=pdf.tags,
                file_size=len(raw_bytes),
                file_type='application/pdf',
            )
            new_file.file.save(output_name, ContentFile(raw_bytes), save=True)

            try:
                new_file.file_hash = new_file.compute_hash()
                new_file.save(update_fields=['file_hash'])
            except Exception:
                pass

            messages.success(request, f'Reordered PDF created: "{output_name}".')
        except Exception as e:
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': f'Could not reorder pages: {e}'}, status=400)
            messages.error(request, f'Could not reorder pages: {e}')

        return redirect('pdf_tools_page')

class PDFToolsPageView(LoginRequiredMixin, TemplateView):
    template_name = 'files/pdf_tools.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user

        all_mine = _editable_files_qs(user).order_by('-created_at')
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

        images = [f for f in SharedFile.objects.filter(id__in=ids)
                  if _can_edit_file(request.user, f)]

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
    flow, no other signers, no emails. The signed copy is saved alongside
    the original as a new SharedFile.
    """
    template_name = 'files/quick_sign.html'

    def _build_file_tree(self, user):
        """
        Returns a structure for the file picker:
          - root_pdfs: PDF files not in any folder
          - folders: list of { folder, pdfs } (only folders that contain PDFs)
        """
        all_pdfs = [
            f for f in _editable_files_qs(user)
            .select_related('folder')
            .order_by('name')
            if getattr(f, 'is_pdf', False)
        ]
        root_pdfs = [f for f in all_pdfs if f.folder_id is None]

        from collections import defaultdict
        by_folder = defaultdict(list)
        for f in all_pdfs:
            if f.folder_id:
                by_folder[f.folder_id].append(f)

        folders = []
        for folder in _visible_folders_qs(user).filter(pk__in=by_folder.keys()).order_by('name'):
            folders.append({
                'folder': folder,
                'pdfs': by_folder[folder.pk],
            })

        return root_pdfs, folders

    def get(self, request, pk=None):
        from apps.files.models import SavedSignature

        selected = None
        if pk:
            selected = get_object_or_404(SharedFile, pk=pk)
            if not _can_edit_file(request.user, selected):
                messages.error(request, 'You do not have permission to sign this file.')
                return redirect('quick_sign')
            if not getattr(selected, 'is_pdf', False):
                messages.error(request, 'Only PDF files can be quick-signed.')
                return redirect('quick_sign')

        root_pdfs, folders = self._build_file_tree(request.user)

        return render(request, self.template_name, {
            'root_pdfs': root_pdfs,
            'folders': folders,
            'selected': selected,
            'default_saved_signature': SavedSignature.objects.filter(
                user=request.user,
                is_default=True
            ).first(),
        })

    def post(self, request, pk=None):
        file_id = request.POST.get('file_id') or (str(pk) if pk else None)
        sig_data = (request.POST.get('signature_data') or '').strip()
        sig_type = (request.POST.get('signature_type') or 'draw').strip()
        sig_page = int(request.POST.get('sig_page', 1))
        sig_x = float(request.POST.get('sig_x', 10))
        sig_y = float(request.POST.get('sig_y', 80))
        sig_w = float(request.POST.get('sig_w', 35))
        sig_h = float(request.POST.get('sig_h', 12))
        output_name = (request.POST.get('output_name') or '').strip()

        if not file_id or not sig_data:
            messages.error(request, 'Please select a file and provide your signature.')
            return redirect('quick_sign')

        pdf = get_object_or_404(SharedFile, pk=file_id)
        if not _can_edit_file(request.user, pdf):
            messages.error(request, 'You do not have permission to sign this file.')
            return redirect('quick_sign')
        if not getattr(pdf, 'is_pdf', False):
            messages.error(request, 'Only PDF files can be quick-signed.')
            return redirect('quick_sign')

        try:
            try:
                from reportlab.pdfgen import canvas as rl_canvas
                from reportlab.lib.utils import ImageReader
            except ImportError:
                messages.error(request, 'reportlab is not installed on this server.')
                return redirect('quick_sign')

            import base64
            from datetime import datetime as _dt

            pdf_bytes = pdf.file.open('rb').read()
            reader = PdfReader(BytesIO(pdf_bytes))
            writer = PdfWriter()

            DS_BLUE = (0.098, 0.376, 0.875)
            DS_INK = (0.063, 0.271, 0.773)
            DS_LABEL = (0.373, 0.420, 0.510)
            STRIP_H = 18.0

            signed_at = _dt.now().strftime('%d %b %Y %H:%M UTC')

            for page_idx in range(len(reader.pages)):
                page = reader.pages[page_idx]
                page_w = float(page.mediabox.width)
                page_h = float(page.mediabox.height)
                page_num = page_idx + 1

                overlay = BytesIO()
                c = rl_canvas.Canvas(overlay, pagesize=(page_w, page_h))

                # Header strip
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
                c.drawRightString(page_w - 5, hdr_y + STRIP_H * 0.28, f'Page {page_num} / {len(reader.pages)}')

                # Footer strip
                c.setFillColorRGB(0.937, 0.953, 1.0)
                c.rect(0, 0, page_w, STRIP_H, stroke=0, fill=1)
                c.setStrokeColorRGB(*DS_BLUE)
                c.setLineWidth(0.8)
                c.line(0, STRIP_H, page_w, STRIP_H)

                c.setFont('Helvetica-Bold', 5.5)
                c.setFillColorRGB(*DS_BLUE)
                c.drawString(5, STRIP_H * 0.28, 'Signed by:')

                c.setFont('Helvetica', 5.5)
                c.setFillColorRGB(0.2, 0.2, 0.2)
                c.drawString(38, STRIP_H * 0.28, request.user.full_name)
                c.drawRightString(page_w - 5, STRIP_H * 0.28, f'Electronically signed \u00B7 {signed_at}')

                # Signature only on selected page
                if page_num == sig_page:
                    fx = (sig_x / 100.0) * page_w
                    fw = (sig_w / 100.0) * page_w
                    fh = (sig_h / 100.0) * page_h
                    fy = page_h * (1.0 - (sig_y + sig_h) / 100.0)

                    label_h = max(9.0, min(fh * 0.24, 16.0)) if fh >= 20 else 0.0
                    sig_h_area = fh - label_h
                    sig_y_area = fy + label_h

                    # NO BLUE BACKGROUND
                    # NO FILLED RECTANGLE
                    # NO BORDER BOX

                    # Drawn / uploaded signature
                    if sig_data.startswith('data:image'):
                        try:
                            _, b64 = sig_data.split(',', 1)
                            raw = base64.b64decode(b64)

                            img_stream = BytesIO(raw)
                            pad = 2

                            c.drawImage(
                                ImageReader(img_stream),
                                fx + pad,
                                sig_y_area + pad,
                                width=max(10, fw - pad * 2),
                                height=max(10, sig_h_area - pad * 2),
                                preserveAspectRatio=True,
                                anchor='c',
                                mask='auto'
                            )
                        except Exception:
                            pass

                    # Typed signature
                    elif sig_type == 'type':
                        display_text = sig_data
                        font_hint = 'Dancing Script'

                        if sig_data.startswith('font:') and '|' in sig_data:
                            parts = sig_data.split('|', 1)
                            font_hint = parts[0][5:]
                            display_text = parts[1]

                        img_buf = None
                        try:
                            img_buf = _typed_sig_to_image(
                                display_text,
                                max(10, fw - 4),
                                max(10, sig_h_area - 4),
                                font_name=font_hint
                            )
                        except Exception:
                            pass

                        if img_buf:
                            pad = 2
                            c.drawImage(
                                ImageReader(img_buf),
                                fx + pad,
                                sig_y_area + pad,
                                width=max(10, fw - pad * 2),
                                height=max(10, sig_h_area - pad * 2),
                                preserveAspectRatio=True,
                                anchor='c',
                                mask='auto'
                            )
                        else:
                            fs = max(8.0, min(sig_h_area * 0.62, 30.0))
                            c.setFont('Helvetica-BoldOblique', fs)
                            c.setFillColorRGB(*DS_INK)
                            c.drawString(fx + 6, sig_y_area + (sig_h_area - fs) * 0.4, display_text)

                    # Optional label text only, without background box
                    # if label_h:
                    #     lf = max(5.5, min(label_h * 0.50, 7.5))
                    #     my = fy + (label_h - lf) * 0.45
                    #
                    #     c.setFont('Helvetica-Bold', lf)
                    #     c.setFillColorRGB(*DS_INK)
                    #     # c.drawString(fx + 3, my, '\u2713')
                    #
                    #     c.setFont('Helvetica', lf)
                    #     c.setFillColorRGB(*DS_LABEL)
                    #     c.saveState()
                    #     p = c.beginPath()
                    #     # p.rect(fx + 3, fy, fw * 0.62, label_h)
                    #     c.clipPath(p, stroke=0, fill=0)
                    #     c.drawString(fx + 3 + lf, my, f' {request.user.full_name[:24]}')
                    #     c.restoreState()
                    #
                    #     c.setFont('Helvetica', lf)
                    #     c.setFillColorRGB(*DS_LABEL)
                    #     # c.drawRightString(fx + fw - 4, my, signed_at[:11])

                c.save()
                overlay.seek(0)
                page.merge_page(PdfReader(overlay).pages[0])
                writer.add_page(page)

            out = BytesIO()
            writer.write(out)
            signed_bytes = out.getvalue()

            base_name = pdf.name[:-4] if pdf.name.lower().endswith('.pdf') else pdf.name
            signed_name = output_name if output_name else f'{base_name}-signed.pdf'
            if not signed_name.lower().endswith('.pdf'):
                signed_name += '.pdf'

            signed_file = SharedFile.objects.create(
                name=signed_name,
                uploaded_by=request.user,
                folder=pdf.folder,
                visibility=pdf.visibility,
                unit=pdf.unit,
                department=pdf.department,
                description=f'Quick-signed copy of "{pdf.name}" by {request.user.full_name}',
                tags=pdf.tags,
                file_size=len(signed_bytes),
                file_type='application/pdf',
            )
            signed_file.file.save(signed_name, ContentFile(signed_bytes), save=True)

            try:
                signed_file.file_hash = signed_file.compute_hash()
                signed_file.save(update_fields=['file_hash'])
            except Exception:
                pass

            file_manager_url = '/files/'
            html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8"/>
  <title>Document Signed</title>
  <meta http-equiv="refresh" content="2;url={file_manager_url}"/>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: var(--eo-bg, #f1f5f9);
      font-family: 'Outfit', 'Segoe UI', sans-serif;
    }}
    .card {{
      background: #fff;
      border-radius: 20px;
      padding: 52px 56px;
      text-align: center;
      box-shadow: 0 8px 40px rgba(0,0,0,.10);
      max-width: 420px;
      width: 92vw;
      animation: pop .35s cubic-bezier(.34,1.56,.64,1) both;
    }}
    @keyframes pop {{
      from {{ opacity:0; transform:scale(.88) translateY(16px); }}
      to   {{ opacity:1; transform:scale(1) translateY(0); }}
    }}
    .check-circle {{
      width: 80px; height: 80px;
      border-radius: 50%;
      background: linear-gradient(135deg,#d1fae5,#6ee7b7);
      display: flex; align-items: center; justify-content: center;
      margin: 0 auto 24px;
      font-size: 2.2rem;
    }}
    h1 {{ font-size: 1.45rem; font-weight: 800; color: #064e3b; margin-bottom: 10px; }}
    .filename {{
      font-size: .88rem; color: #475569; margin-bottom: 28px;
      background: #f8fafc; border-radius: 8px; padding: 8px 14px;
      font-weight: 600; word-break: break-all;
    }}
    .redirect-msg {{
      font-size: .82rem; color: #94a3b8; margin-top: 10px;
    }}
    .bar-wrap {{
      height: 4px; background: #e2e8f0; border-radius: 99px;
      margin: 18px 0 0; overflow: hidden;
    }}
    .bar {{
      height: 100%;
      background: linear-gradient(90deg,#10b981,#34d399);
      border-radius: 99px;
      width: 100%;
      animation: shrink 2s linear forwards;
    }}
    @keyframes shrink {{ from {{ width:100%; }} to {{ width:0%; }} }}
    a.btn {{
      display: inline-block; margin-top: 20px;
      padding: 10px 24px;
      background: #10b981; color: #fff;
      border-radius: 10px; text-decoration: none;
      font-weight: 700; font-size: .88rem;
      transition: background .15s;
    }}
    a.btn:hover {{ background: #059669; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="check-circle">✅</div>
    <h1>Document Signed!</h1>
    <div class="filename">📄 {signed_name}</div>
    <div class="redirect-msg">Taking you to File Manager in <span id="cnt">2</span>s…</div>
    <div class="bar-wrap"><div class="bar"></div></div>
    <a href="{file_manager_url}" class="btn">Go to Files now →</a>
  </div>
  <script>
    var t = 2;
    var el = document.getElementById('cnt');
    var iv = setInterval(function() {{
      t--;
      if (el) el.textContent = t;
      if (t <= 0) {{
        clearInterval(iv);
        window.location.href = '{file_manager_url}';
      }}
    }}, 1000);
  </script>
</body>
</html>"""
            return HttpResponse(html)

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
    """Return (root_pdfs, folders_with_pdfs) for the PDF picker.
    Includes files the user owns AND files shared with edit/full permission."""
    from collections import defaultdict
    all_pdfs = [
        f for f in _editable_files_qs(user)
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

        pdf = get_object_or_404(SharedFile, pk=file_id)
        if not _can_edit_file(request.user, pdf):
            messages.error(request, 'You do not have permission to convert this file.')
            return redirect('pdf_to_word')
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

        pdf = get_object_or_404(SharedFile, pk=file_id)
        if not _can_edit_file(request.user, pdf):
            messages.error(request, 'You do not have permission to convert this file.')
            return redirect('pdf_to_image')
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

class ZipExtractView(LoginRequiredMixin, TemplateView):
    template_name = 'files/zip_extract.html'
    MAX_FILE_SIZE = 50 * 1024 * 1024      # 50 MB per extracted file
    MAX_TOTAL_SIZE = 300 * 1024 * 1024    # 300 MB total extracted size
    MAX_FILE_COUNT = 500

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user

        editable_files = _editable_files_qs(user).filter(
            name__iendswith='.zip',
            is_latest=True
        ).select_related('folder', 'uploaded_by').order_by('-created_at')

        visible_folders = _visible_folders_qs(user).order_by('name')

        selected_zip = None
        zip_entries = []
        selected_zip_id = self.request.GET.get('file', '').strip()

        if selected_zip_id:
            try:
                selected_zip = editable_files.get(id=selected_zip_id)
                zip_entries = self._read_zip_entries(selected_zip)
            except SharedFile.DoesNotExist:
                selected_zip = None
            except zipfile.BadZipFile:
                messages.error(self.request, 'That ZIP file is invalid or corrupted.')

        ctx.update({
            'zip_files': editable_files,
            'all_folders': visible_folders,
            'selected_zip': selected_zip,
            'zip_entries': zip_entries,
        })
        return ctx

    def post(self, request, *args, **kwargs):
        user = request.user
        zip_file_id = request.POST.get('zip_file_id', '').strip()
        target_folder_id = request.POST.get('target_folder_id', '').strip()
        create_subfolder = request.POST.get('create_subfolder') == '1'

        zip_file = get_object_or_404(
            _editable_files_qs(user).filter(name__iendswith='.zip', is_latest=True),
            id=zip_file_id
        )

        target_folder = None
        if target_folder_id:
            target_folder = get_object_or_404(FileFolder, id=target_folder_id)
            if not _can_edit_folder(user, target_folder):
                messages.error(request, 'You do not have permission to extract into that folder.')
                return redirect('zip_extract')

        try:
            extracted_count, skipped_count, saved_folder = self._extract_zip_to_files(
                request=request,
                zip_shared_file=zip_file,
                target_folder=target_folder,
                create_subfolder=create_subfolder,
            )
        except zipfile.BadZipFile:
            messages.error(request, 'The selected ZIP file is invalid or corrupted.')
            return redirect('zip_extract')
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect(f"{reverse('zip_extract')}?file={zip_file.id}")
        except Exception as exc:
            messages.error(request, f'ZIP extraction failed: {exc}')
            return redirect(f"{reverse('zip_extract')}?file={zip_file.id}")

        msg = f'Extracted {extracted_count} file'
        if extracted_count != 1:
            msg += 's'
        if skipped_count:
            msg += f' ({skipped_count} skipped)'
        if saved_folder:
            msg += f' into "{saved_folder.name}"'
        msg += '.'

        messages.success(request, msg)
        return redirect('file_manager')

    def _read_zip_entries(self, shared_file):
        entries = []
        total_size = 0

        with shared_file.file.open('rb') as fh:
            with zipfile.ZipFile(fh) as zf:
                for info in zf.infolist():
                    safe_name = _safe_zip_member_name(info.filename)
                    if not safe_name:
                        continue

                    is_dir = info.is_dir()
                    if not is_dir:
                        total_size += int(info.file_size or 0)

                    entries.append({
                        'name': safe_name,
                        'is_dir': is_dir,
                        'size': info.file_size,
                        'compressed_size': info.compress_size,
                    })

        return entries

    def _extract_zip_to_files(self, request, zip_shared_file, target_folder=None, create_subfolder=False):
        archive_name_root = os.path.splitext(zip_shared_file.name)[0].strip() or 'Extracted ZIP'
        destination_folder = target_folder

        if create_subfolder:
            destination_folder = FileFolder.objects.create(
                name=_unique_folder_name(archive_name_root, target_folder, request.user),
                owner=request.user,
                parent=target_folder,
                visibility=FileFolder.Visibility.PRIVATE,
                unit=None,
                department=None,
                color='#f59e0b',
            )

        extracted_count = 0
        skipped_count = 0
        running_total_size = 0

        with zip_shared_file.file.open('rb') as fh:
            with zipfile.ZipFile(fh) as zf:
                infos = zf.infolist()

                if len(infos) > self.MAX_FILE_COUNT:
                    raise ValueError(f'ZIP contains too many items. Limit is {self.MAX_FILE_COUNT}.')

                with transaction.atomic():
                    for info in infos:
                        safe_name = _safe_zip_member_name(info.filename)
                        if not safe_name:
                            skipped_count += 1
                            continue

                        if info.is_dir():
                            continue

                        file_size = int(info.file_size or 0)
                        if file_size <= 0:
                            skipped_count += 1
                            continue

                        if file_size > self.MAX_FILE_SIZE:
                            skipped_count += 1
                            continue

                        running_total_size += file_size
                        if running_total_size > self.MAX_TOTAL_SIZE:
                            raise ValueError(
                                f'ZIP extraction exceeds total allowed size of {self._human_size(self.MAX_TOTAL_SIZE)}.'
                            )

                        try:
                            with zf.open(info, 'r') as extracted_fp:
                                raw = extracted_fp.read()
                        except Exception:
                            skipped_count += 1
                            continue

                        if len(raw) != file_size:
                            file_size = len(raw)

                        base_name = os.path.basename(safe_name)
                        if not base_name:
                            skipped_count += 1
                            continue

                        final_name = _unique_file_name_for_folder(base_name, destination_folder)

                        new_file = SharedFile(
                            name=final_name,
                            folder=destination_folder,
                            uploaded_by=request.user,
                            visibility=SharedFile.Visibility.PRIVATE,
                            unit=None,
                            department=None,
                            file_size=file_size,
                            file_type=_guess_content_type(final_name),
                            description=f'Extracted from ZIP: {zip_shared_file.name}',
                            tags='zip, extracted',
                            is_latest=True,
                        )
                        new_file.file.save(final_name, ContentFile(raw), save=False)
                        new_file.file_hash = hashlib.sha256(raw).hexdigest()
                        new_file.save()

                        _log_file_history(
                            new_file,
                            FileHistory.Action.CREATED,
                            actor=request.user,
                            notes=f'Extracted from ZIP "{zip_shared_file.name}"'
                        )

                        extracted_count += 1

        return extracted_count, skipped_count, destination_folder


def _unique_folder_name(base_name, parent_folder, owner):
    candidate = base_name
    counter = 1

    while FileFolder.objects.filter(
        owner=owner,
        parent=parent_folder,
        name=candidate
    ).exists():
        candidate = f'{base_name} ({counter})'
        counter += 1

    return candidate

# ─────────────────────────────────────────────────────────────────────────────
# Pin / Unpin
# ─────────────────────────────────────────────────────────────────────────────

class PinToggleView(LoginRequiredMixin, View):
    """Toggle pin state for a file or folder. Returns JSON."""
    def post(self, request):
        item_type = request.POST.get('type')   # 'file' or 'folder'
        item_id   = request.POST.get('id', '').strip()
        user      = request.user

        if item_type == 'file':
            obj = get_object_or_404(SharedFile, pk=item_id)
            if not _file_permission_for(user, obj):
                return JsonResponse({'error': 'Permission denied'}, status=403)
            existing = FilePinnedItem.objects.filter(user=user, file=obj).first()
            if existing:
                existing.delete()
                return JsonResponse({'pinned': False})
            FilePinnedItem.objects.create(user=user, file=obj)
            return JsonResponse({'pinned': True})

        elif item_type == 'folder':
            obj = get_object_or_404(FileFolder, pk=item_id)
            if not _folder_permission_for(user, obj):
                return JsonResponse({'error': 'Permission denied'}, status=403)
            existing = FilePinnedItem.objects.filter(user=user, folder=obj).first()
            if existing:
                existing.delete()
                return JsonResponse({'pinned': False})
            FilePinnedItem.objects.create(user=user, folder=obj)
            return JsonResponse({'pinned': True})

        return JsonResponse({'error': 'Invalid type'}, status=400)


# ─────────────────────────────────────────────────────────────────────────────
# Public (no-login) file download via token
# ─────────────────────────────────────────────────────────────────────────────

class FilePublicDownloadView(View):
    """Download a file via a public token — no login required."""
    def get(self, request, token):
        pt = get_object_or_404(FilePublicToken, token=token)
        if pt.is_expired:
            from django.http import HttpResponse
            return HttpResponse('This download link has expired.', status=410)
        f = pt.file
        pt.download_count += 1
        pt.save(update_fields=['download_count'])
        import mimetypes as _mimetypes
        content_type, _ = _mimetypes.guess_type(f.name)
        response = FileResponse(f.file.open('rb'), content_type=content_type or 'application/octet-stream')
        response['Content-Disposition'] = f'attachment; filename="{f.name}"'
        return response


# ─────────────────────────────────────────────────────────────────────────────
# Signature view-only (CC viewer role — no login, no signing)
# ─────────────────────────────────────────────────────────────────────────────

class SignatureViewOnlyView(View):
    """Let a CC viewer see the signing status without login."""
    def get(self, request, token):
        cc  = get_object_or_404(SignatureCC, view_token=token)
        req = cc.request
        return render(request, 'files/signature_view_only.html', {
            'sig_req':  req,
            'signers':  req.signers.all(),
            'cc':       cc,
            'audit':    req.audit_trail.order_by('timestamp')[:20],
        })

class FileRenameView(LoginRequiredMixin, View):
    def post(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)

        if not _can_edit_file(request.user, f):
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)
            messages.error(request, 'You do not have permission to rename this file.')
            return redirect('file_manager')

        new_name = (request.POST.get('name') or '').strip()
        if not new_name:
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': 'File name cannot be empty.'}, status=400)
            messages.error(request, 'File name cannot be empty.')
            return redirect(request.POST.get('next') or 'file_manager')

        old_name = f.name
        if new_name == old_name:
            if _is_ajax(request):
                return JsonResponse({'ok': True, 'message': 'Name unchanged.', 'name': new_name})
            messages.info(request, 'File name was not changed.')
            return redirect(request.POST.get('next') or 'file_manager')

        # preserve extension if user removed it
        old_root, old_ext = os.path.splitext(old_name)
        new_root, new_ext = os.path.splitext(new_name)
        if old_ext and not new_ext:
            new_name = new_name + old_ext

        # avoid duplicate latest file names in same folder
        qs = SharedFile.objects.filter(
            folder=f.folder,
            name=new_name,
            is_latest=True
        ).exclude(pk=f.pk)
        if qs.exists():
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': f'A file named "{new_name}" already exists here.'}, status=400)
            messages.error(request, f'A file named "{new_name}" already exists in this location.')
            return redirect(request.POST.get('next') or 'file_manager')

        f.name = new_name
        f.save(update_fields=['name', 'updated_at'])

        _log_file_history(
            f,
            FileHistory.Action.RENAMED,
            actor=request.user,
            notes=f'Renamed from "{old_name}" to "{new_name}"'
        )

        messages.success(request, f'File renamed to "{new_name}".')

        if _is_ajax(request):
            return JsonResponse({'ok': True, 'message': f'File renamed to "{new_name}".', 'name': new_name, 'file_id': str(pk)})

        next_url = request.POST.get('next', '')
        return redirect(next_url if next_url.startswith('/') else 'file_manager')


class FolderRenameView(LoginRequiredMixin, View):
    def post(self, request, pk):
        folder = get_object_or_404(FileFolder, pk=pk)

        if not _can_edit_folder(request.user, folder):
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': 'Permission denied.'}, status=403)
            messages.error(request, 'You do not have permission to rename this folder.')
            return redirect('file_manager')

        new_name = (request.POST.get('name') or '').strip()
        if not new_name:
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': 'Folder name cannot be empty.'}, status=400)
            messages.error(request, 'Folder name cannot be empty.')
            return redirect(request.POST.get('next') or 'file_manager')

        old_name = folder.name
        if new_name == old_name:
            if _is_ajax(request):
                return JsonResponse({'ok': True, 'message': 'Name unchanged.', 'name': new_name})
            messages.info(request, 'Folder name was not changed.')
            return redirect(request.POST.get('next') or 'file_manager')

        exists = FileFolder.objects.filter(
            owner=folder.owner,
            parent=folder.parent,
            name=new_name
        ).exclude(pk=folder.pk).exists()

        if exists:
            if _is_ajax(request):
                return JsonResponse({'ok': False, 'error': f'A folder named "{new_name}" already exists here.'}, status=400)
            messages.error(request, f'A folder named "{new_name}" already exists here.')
            return redirect(request.POST.get('next') or 'file_manager')

        folder.name = new_name
        folder.save(update_fields=['name'])

        messages.success(request, f'Folder renamed to "{new_name}".')

        if _is_ajax(request):
            return JsonResponse({'ok': True, 'message': f'Folder renamed to "{new_name}".', 'name': new_name, 'folder_id': str(pk)})

        next_url = request.POST.get('next', '')
        return redirect(next_url if next_url.startswith('/') else 'file_manager')


def _annotate_note_badges(files_qs, user):
    """
    Annotate a SharedFile queryset with two boolean fields:

    has_my_note      — True when THIS user has written a non-empty note on the file
    has_shared_note  — True when ANOTHER user has shared their note with this user
                       (via person / unit / department / office scope)
    """
    profile = getattr(user, 'staffprofile', None)
    unit = profile.unit if profile else None
    dept = profile.department if profile else None

    # ── has_my_note ──────────────────────────────────────────────────────────
    # A note exists, was written by this user, and has non-empty body.
    my_note_qs = FileNote.objects.filter(
        file=OuterRef('pk'),
        author=user,
    ).exclude(body='')

    # ── has_shared_note ───────────────────────────────────────────────────────
    # A FileNoteShare exists for a note on this file, shared with this user
    # (by someone else), and that note has non-empty body.
    share_q = Q(scope='office') | Q(scope='person', shared_with_user=user)
    if unit:
        share_q |= Q(scope='unit', unit=unit)
    if dept:
        share_q |= Q(scope='department', department=dept)

    shared_note_qs = FileNoteShare.objects.filter(
        share_q,
        note__file=OuterRef('pk'),
    ).exclude(
        note__author=user  # don't count own note shared back to self
    ).exclude(
        note__body=''  # ignore empty notes
    )

    return files_qs.annotate(
        has_my_note=Exists(my_note_qs),
        has_shared_note=Exists(shared_note_qs),
    )

class FileNoteView(LoginRequiredMixin, View):
    """
    GET  /files/<pk>/note/   → own note body + notes others have shared with me
    POST /files/<pk>/note/   → upsert own note body
    """

    def get(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)
        if not _file_permission_for(request.user, f):
            return JsonResponse({'error': 'Permission denied'}, status=403)

        note = FileNote.objects.filter(file=f, author=request.user).first()
        shared = _get_shared_notes_for(request.user, f)

        return JsonResponse({
            'body': note.body if note else '',
            'updated_at': note.updated_at.isoformat() if note else None,
            'shared_notes': shared,
        })

    def post(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)
        if not _file_permission_for(request.user, f):
            return JsonResponse({'error': 'Permission denied'}, status=403)

        body = request.POST.get('body', '')
        note, _ = FileNote.objects.update_or_create(
            file=f, author=request.user,
            defaults={'body': body},
        )
        return JsonResponse({
            'ok': True,
            'body': note.body,
            'updated_at': note.updated_at.isoformat(),
        })


TYPING_WINDOW_SECONDS = 5  # consider "typing" if ping received within last 5 s


def _get_shared_notes_for(user, file_obj):
    """
    Return notes that OTHER users have explicitly shared with `user` on this file.
    Each entry now includes `is_typing` — True if the author pinged within 5 s.
    """
    profile = getattr(user, 'staffprofile', None)
    unit = profile.unit if profile else None
    dept = profile.department if profile else None

    q = Q(scope='office') | Q(scope='person', shared_with_user=user)
    if unit:
        q |= Q(scope='unit', unit=unit)
    if dept:
        q |= Q(scope='department', department=dept)

    shares = (
        FileNoteShare.objects
        .filter(note__file=file_obj)
        .exclude(note__author=user)
        .filter(q)
        .select_related('note__author')
        .order_by('note__author_id', '-note__updated_at')
        .distinct()
    )

    now = timezone.now()
    cutoff = now - timedelta(seconds=TYPING_WINDOW_SECONDS)

    result = []
    seen = set()
    for share in shares:
        nid = share.note_id
        if nid in seen:
            continue
        seen.add(nid)
        n = share.note
        result.append({
            'author': n.author.full_name,
            'body': n.body,
            'updated_at': n.updated_at.isoformat(),
            'is_typing': bool(n.typing_at and n.typing_at >= cutoff),
        })
    return result


class FileNoteShareView(LoginRequiredMixin, View):
    """
    GET    /<pk>/note/share/             → list shares for my note on this file
    POST   /<pk>/note/share/             → add share(s)
    DELETE /<pk>/note/share/<share_id>/  → revoke one share
    POST   /<pk>/note/share/revoke_all/  → wipe all shares (stop-sharing hook)
    """

    def _get_note(self, request, pk):
        """Return (note, error_response). Creates note if it doesn't exist yet."""
        f = get_object_or_404(SharedFile, pk=pk)
        if not _file_permission_for(request.user, f):
            return None, JsonResponse({'error': 'Permission denied'}, status=403)
        note, _ = FileNote.objects.get_or_create(
            file=f, author=request.user,
            defaults={'body': ''}
        )
        return note, None

    def get(self, request, pk, share_id=None):
        note, err = self._get_note(request, pk)
        if err:
            return err
        shares = note.shares.select_related('shared_with_user', 'unit', 'department')
        data = [{
            'id': str(s.id),
            'scope': s.scope,
            'scope_display': s.scope_display,
            'label': s.label,
        } for s in shares]
        return JsonResponse({'shares': data})

    def post(self, request, pk, share_id=None):
        # Sub-action: revoke_all — path ends in /revoke_all/
        if request.path.rstrip('/').endswith('revoke_all'):
            return self._revoke_all(request, pk)

        note, err = self._get_note(request, pk)
        if err:
            return err

        try:
            payload = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        scope = payload.get('scope', '')

        if scope == 'office':
            FileNoteShare.objects.get_or_create(
                note=note, scope='office',
                defaults={'shared_with_user': None, 'unit': None, 'department': None}
            )

        elif scope == 'unit':
            from apps.organization.models import Unit
            unit = get_object_or_404(Unit, pk=payload.get('unit_id'))
            FileNoteShare.objects.get_or_create(
                note=note, scope='unit', unit=unit,
                defaults={'shared_with_user': None, 'department': None}
            )

        elif scope in ('dept', 'department'):
            from apps.organization.models import Department
            dept = get_object_or_404(Department, pk=payload.get('dept_id'))
            FileNoteShare.objects.get_or_create(
                note=note, scope='department', department=dept,
                defaults={'shared_with_user': None, 'unit': None}
            )

        elif scope == 'person':
            from django.contrib.auth import get_user_model
            User = get_user_model()
            for uid in payload.get('user_ids', []):
                try:
                    target = User.objects.get(pk=uid)
                    FileNoteShare.objects.get_or_create(
                        note=note, scope='person', shared_with_user=target,
                        defaults={'unit': None, 'department': None}
                    )
                except User.DoesNotExist:
                    pass
        else:
            return JsonResponse({'error': 'Invalid scope: ' + scope}, status=400)

        return JsonResponse({'ok': True})

    def delete(self, request, pk, share_id=None):
        note, err = self._get_note(request, pk)
        if err:
            return err
        if not share_id:
            return JsonResponse({'error': 'share_id required'}, status=400)
        share = get_object_or_404(FileNoteShare, pk=share_id, note=note)
        share.delete()
        return JsonResponse({'ok': True})

    def _revoke_all(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)
        if not _file_permission_for(request.user, f):
            return JsonResponse({'error': 'Permission denied'}, status=403)
        FileNoteShare.objects.filter(
            note__file=f, note__author=request.user
        ).delete()
        return JsonResponse({'ok': True})


class FileNoteTypingView(LoginRequiredMixin, View):
    """
    POST /files/<pk>/note/typing/
    Updates typing_at timestamp on the author's note.
    Returns 204 No Content — callers should fire-and-forget.
    """

    def post(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)
        if not _file_permission_for(request.user, f):
            return JsonResponse({'error': 'Permission denied'}, status=403)

        FileNote.objects.update_or_create(
            file=f, author=request.user,
            defaults={'typing_at': timezone.now()}
        )
        return HttpResponse(status=204)

class FileNoteBulkStatusView(LoginRequiredMixin, View):
    """
    GET /files/note/bulk-status/?ids=uuid1,uuid2,...
    Returns badge states for a list of file IDs in one query.
    Called once on page load so badges are correct after a refresh.
    """

    def get(self, request):
        raw = request.GET.get('ids', '')
        if not raw:
            return JsonResponse({'statuses': {}})

        # Parse and deduplicate IDs — ignore anything that isn't a valid UUID
        import re
        UUID_RE = re.compile(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            re.IGNORECASE
        )
        ids = [i.strip() for i in raw.split(',') if UUID_RE.match(i.strip())]
        if not ids:
            return JsonResponse({'statuses': {}})

        user    = request.user
        profile = getattr(user, 'staffprofile', None)
        unit    = profile.unit       if profile else None
        dept    = profile.department if profile else None

        # Files the user can actually see (security check)
        visible_ids = set(
            str(i) for i in
            _visible_files_qs(user)
            .filter(id__in=ids)
            .values_list('id', flat=True)
        )

        # Own non-empty notes
        my_note_ids = set(
            str(i) for i in
            FileNote.objects
            .filter(file_id__in=visible_ids, author=user)
            .exclude(body='')
            .values_list('file_id', flat=True)
        )

        # Shared notes from others
        share_q = Q(scope='office') | Q(scope='person', shared_with_user=user)
        if unit:
            share_q |= Q(scope='unit', unit=unit)
        if dept:
            share_q |= Q(scope='department', department=dept)

        shared_note_ids = set(
            str(i) for i in
            FileNoteShare.objects
            .filter(share_q, note__file_id__in=visible_ids)
            .exclude(note__author=user)
            .exclude(note__body='')
            .values_list('note__file_id', flat=True)
            .distinct()
        )

        statuses = {}
        for fid in visible_ids:
            has_mine   = fid in my_note_ids
            has_shared = fid in shared_note_ids
            if has_mine or has_shared:
                statuses[fid] = {
                    'has_my_note':     has_mine,
                    'has_shared_note': has_shared,
                }

        return JsonResponse({'statuses': statuses})

class LetterheadApplyToolView(LoginRequiredMixin, TemplateView):
    template_name = 'files/letterhead_apply_tool.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user

        editable_files = _editable_files_qs(user).select_related('folder').order_by('-created_at')
        pdf_files = editable_files.filter(file_type='application/pdf').order_by('-created_at')

        preselected_file = None
        file_id = self.request.GET.get('file')
        if file_id:
            try:
                preselected_file = editable_files.get(pk=file_id)
            except SharedFile.DoesNotExist:
                pass

        active_letterhead = LetterheadTemplate.get_active()

        ctx.update({
            'files': editable_files,
            'pdf_files': pdf_files,
            'preselected_file': preselected_file,
            'active_letterhead': active_letterhead,
        })
        return ctx

    def post(self, request, *args, **kwargs):
        user = request.user

        document_id = request.POST.get('document_id')
        letterhead_file_id = request.POST.get('letterhead_file_id')
        apply_mode = request.POST.get('apply_mode', 'first')
        result_name = (request.POST.get('result_name') or '').strip()
        send_for_signature = request.POST.get('send_for_signature') == '1'
        open_mode = request.POST.get('open_mode', 'preview')

        if not document_id:
            messages.error(request, 'Please select a document.')
            return redirect('letterhead_apply_tool')

        if not letterhead_file_id:
            messages.error(request, 'Please select a letterhead PDF from File Manager.')
            return redirect('letterhead_apply_tool')

        source_doc = get_object_or_404(SharedFile, pk=document_id)
        letterhead_file = get_object_or_404(SharedFile, pk=letterhead_file_id)

        if not _can_edit_file(user, source_doc):
            messages.error(request, 'You do not have permission to use this document.')
            return redirect('file_manager')

        if not letterhead_file.is_pdf:
            messages.error(request, 'Selected letterhead must be a PDF.')
            return redirect('letterhead_apply_tool')

        if not getattr(source_doc, 'file', None):
            messages.error(request, 'The selected source document has no file attached.')
            return redirect('letterhead_apply_tool')

        if not getattr(letterhead_file, 'file', None):
            messages.error(request, 'The selected letterhead file has no file attached.')
            return redirect('letterhead_apply_tool')

        src_tmp = None
        letter_tmp = None

        try:
            pdf_doc = _ensure_pdf_shared_file(source_doc, user)

            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as srcf:
                src_tmp = srcf.name
                for chunk in pdf_doc.file.chunks():
                    srcf.write(chunk)

            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as ltf:
                letter_tmp = ltf.name
                for chunk in letterhead_file.file.chunks():
                    ltf.write(chunk)

            x_pct = request.POST.get('x_pct', '10')
            y_pct = request.POST.get('y_pct', '18')
            width_pct = request.POST.get('width_pct', '80')
            height_pct = request.POST.get('height_pct', '70')

            merged_buffer = _apply_pdf_letterhead(
                src_tmp,
                letter_tmp,
                apply_mode=apply_mode,
                x_pct=x_pct,
                y_pct=y_pct,
                width_pct=width_pct,
                height_pct=height_pct,
            )

            if result_name:
                final_name = result_name if result_name.lower().endswith('.pdf') else result_name + '.pdf'
            else:
                source_name = (
                    source_doc.name
                    or os.path.basename(getattr(source_doc.file, 'name', '') or '')
                    or f'document-{source_doc.pk}'
                )
                base_name = os.path.splitext(source_name)[0] or f'document-{source_doc.pk}'
                final_name = f'{base_name} - with letterhead.pdf'

            final_name = _unique_file_name_for_folder(source_doc.folder, final_name)

            new_file = SharedFile.objects.create(
                name=final_name,
                uploaded_by=user,
                folder=source_doc.folder,
                visibility=source_doc.visibility,
                description=f'Generated with letterhead from {letterhead_file.name or "selected letterhead"}',
                tags=source_doc.tags,
                file_size=merged_buffer.getbuffer().nbytes,
                file_type='application/pdf',
            )
            new_file.file.save(final_name, ContentFile(merged_buffer.read()), save=True)

            try:
                new_file.file_hash = new_file.compute_hash()
                new_file.save(update_fields=['file_hash'])
            except Exception:
                pass

            _log_file_history(
                new_file,
                action='created',
                actor=user,
                notes=f'Applied letterhead PDF to {source_doc.name or source_doc.pk}'
            )

            if send_for_signature:
                return redirect('create_signature_request', pk=new_file.pk)

            if open_mode == 'download':
                return redirect('file_download', pk=new_file.pk)

            return redirect('file_preview', pk=new_file.pk)

        except Exception as e:
            messages.error(request, f'Could not create the final PDF: {e}')
            return redirect('letterhead_apply_tool')

        finally:
            try:
                if src_tmp and os.path.exists(src_tmp):
                    os.unlink(src_tmp)
            except Exception:
                pass
            try:
                if letter_tmp and os.path.exists(letter_tmp):
                    os.unlink(letter_tmp)
            except Exception:
                pass