"""
apps/files/tasks.py
───────────────────
Thread-based background tasks for the Files app signing flow.

This is the Celery-free variant: tasks run in daemon threads spawned from
the request handler. No broker, no worker process, no infrastructure to
operate. Trade-offs vs Celery:

  + Zero ops: works immediately, no Redis/RabbitMQ, no `celery worker`.
  + Easy to reason about — same Python process, same imports, same
    Django connection pool.
  - Tasks are lost if the web process restarts (gunicorn reload, OOM,
    deploy) while a thread is mid-flight.
  - No retry, no visibility — failures only show up in logs.
  - Each thread holds a DB connection from the per-process pool while
    it runs. With many concurrent signs and slow SMTP, you can saturate
    the pool. (Mitigated below by closing connections at task end.)

Public API matches the Celery version so the call sites in views.py
don't change:

    from apps.files.tasks import (
        finalise_signature_after_sign,
        notify_signature_declined,
    )
    finalise_signature_after_sign.delay(sig_req_id=..., signer_id=..., base_url=...)

Both tasks expose `.delay(...)` for parity with Celery and a callable form
for the inline-fallback path in views.py.
"""
from __future__ import annotations

import logging
import threading

from django.db import close_old_connections

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Thin "task" wrapper that mimics the bits of @shared_task we actually use
# ─────────────────────────────────────────────────────────────────────────────

class _ThreadedTask:
    """
    Wraps a function so it can be called either:
      • inline: `task(arg1, arg2)` — runs synchronously, used as a fallback
        when the spawning machinery is unavailable.
      • backgrounded: `task.delay(arg1, arg2)` — fires a daemon thread and
        returns immediately.

    Each backgrounded run wraps the call in close_old_connections() so the
    thread doesn't permanently hog a DB connection from the pool.
    """

    def __init__(self, fn):
        self.fn = fn
        self.name = f'{fn.__module__}.{fn.__name__}'

    def __call__(self, *args, **kwargs):
        return self.fn(*args, **kwargs)

    def delay(self, *args, **kwargs):
        thread = threading.Thread(
            target=self._run,
            args=args,
            kwargs=kwargs,
            name=f'task:{self.fn.__name__}',
            daemon=True,
        )
        thread.start()
        return thread

    def _run(self, *args, **kwargs):
        # Recycle stale DB connections inherited from the parent thread.
        close_old_connections()
        try:
            self.fn(*args, **kwargs)
        except Exception:
            logger.exception('Background task %s failed', self.name)
        finally:
            # Release the connection this thread acquired so it goes back
            # to the pool instead of being held until garbage collection.
            close_old_connections()


def _task(fn):
    """Decorator — turns a plain function into a _ThreadedTask."""
    return _ThreadedTask(fn)


# ─────────────────────────────────────────────────────────────────────────────
# Per-signature finalisation  (same logic as the Celery version)
# ─────────────────────────────────────────────────────────────────────────────

@_task
def finalise_signature_after_sign(sig_req_id, signer_id, base_url):
    """
    Run the post-signing work that doesn't need to block the redirect:

      • Email the creator that this signer just signed.
      • If the request is "ordered", invite the next pending signer.
      • If every signer has now signed, embed signatures into the PDF
        and send the completion email + CC notifications.

    Idempotent: if the request is already 'completed' when this runs,
    the embedding/email steps are skipped.
    """
    # Late imports — keep this module import-light so views.py doesn't
    # drag in ReportLab/pypdf at startup.
    from apps.files.models import SignatureRequest, SignatureRequestSigner
    from apps.files import views as files_views

    try:
        sig_req = SignatureRequest.objects.get(pk=sig_req_id)
    except SignatureRequest.DoesNotExist:
        logger.warning('finalise_signature_after_sign: request %s gone', sig_req_id)
        return

    try:
        signer = SignatureRequestSigner.objects.get(pk=signer_id)
    except SignatureRequestSigner.DoesNotExist:
        signer = None

    # ── 1) "Someone signed" email + in-app ping to creator ─────────────────
    if signer and sig_req.created_by_id and sig_req.created_by_id != signer.user_id:
        try:
            files_views._notify(
                sig_req.created_by, 'sign_signed',
                title=f'{signer.name} signed your document',
                body=sig_req.title,
                link=f'/files/signatures/{sig_req.pk}/',
            )
        except Exception:
            logger.exception('Could not send sign_signed notification for %s', sig_req.pk)

        try:
            from django.conf import settings
            from django.core.mail import send_mail
            send_mail(
                subject=f'[EasyOffice] {signer.name} signed "{sig_req.title}"',
                message=(
                    f'Hello {sig_req.created_by.full_name},\n\n'
                    f'{signer.name} has signed your document "{sig_req.title}".\n\n'
                    f'View the audit trail:\n/files/signatures/{sig_req.pk}/\n\n'
                    f'— EasyOffice'
                ),
                from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@easyoffice.local'),
                recipient_list=[sig_req.created_by.email] if sig_req.created_by.email else [],
                fail_silently=True,
            )
        except Exception:
            logger.exception('Could not send sign_signed email for %s', sig_req.pk)

    # ── 2) Ordered signing: invite next pending signer ─────────────────────
    try:
        if getattr(sig_req, 'ordered_signing', False):
            files_views._notify_next_pending_signer(sig_req, base_url)
    except Exception:
        logger.exception('Could not notify next pending signer for %s', sig_req.pk)

    # ── 3) If everyone has now signed, stamp the PDF and email everyone ────
    sig_req.refresh_from_db()
    all_signed = not sig_req.signers.exclude(status='signed').exists()
    if not all_signed:
        return

    try:
        files_views._embed_signatures_in_pdf(sig_req)
        sig_req.refresh_from_db(fields=['document', 'status', 'completed_at', 'updated_at'])
    except Exception:
        logger.exception('Could not embed signatures for %s', sig_req.pk)

    try:
        if hasattr(sig_req, 'update_status'):
            sig_req.update_status()
    except Exception:
        logger.exception('Could not update_status() for %s', sig_req.pk)

    sig_req.refresh_from_db()
    if getattr(sig_req, 'status', None) == 'completed':
        try:
            files_views._log_audit(sig_req, 'completed')
        except Exception:
            logger.exception('Could not log completion audit for %s', sig_req.pk)

        try:
            files_views._notify(
                sig_req.created_by,
                'sign_completed',
                title=f'All signatures collected: {sig_req.title}',
                body=f'{sig_req.signers.count()} signer(s) have all signed.',
                link=f'/files/signatures/{sig_req.pk}/',
            )
        except Exception:
            logger.exception('Could not send completion notification for %s', sig_req.pk)

        try:
            files_views._send_completion_email(sig_req, base_url)
        except Exception:
            logger.exception('Could not send completion email for %s', sig_req.pk)

        try:
            for cc in sig_req.cc_recipients.all():
                try:
                    files_views._notify_cc_recipient(cc, base_url, event='completed')
                except Exception:
                    logger.exception('Could not notify CC %s for %s', cc.pk, sig_req.pk)
        except Exception:
            logger.exception('Could not iterate CC recipients for %s', sig_req.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Decline notification
# ─────────────────────────────────────────────────────────────────────────────

@_task
def notify_signature_declined(sig_req_id, signer_id, base_url):
    """
    Send the styled "❌ Signing Declined" HTML email to the creator
    plus the in-app notification, both in the background.
    """
    from django.conf import settings
    from django.core.mail import EmailMessage as _EM
    from apps.files.models import SignatureRequest, SignatureRequestSigner
    from apps.files import views as files_views

    try:
        sig_req = SignatureRequest.objects.get(pk=sig_req_id)
        signer = SignatureRequestSigner.objects.get(pk=signer_id)
    except (SignatureRequest.DoesNotExist, SignatureRequestSigner.DoesNotExist):
        return

    if not sig_req.created_by_id or sig_req.created_by_id == signer.user_id:
        return

    # In-app notification.
    try:
        reason_suffix = f' Reason: {signer.decline_reason}' if signer.decline_reason else ''
        files_views._notify(
            sig_req.created_by, 'sign_declined',
            title=f'{signer.name} declined to sign',
            body=f'{sig_req.title}.{reason_suffix}',
            link=f'/files/signatures/{sig_req.pk}/',
        )
    except Exception:
        logger.exception('Could not send sign_declined notification for %s', sig_req.pk)

    # Styled HTML email.
    try:
        _org = getattr(settings, 'ORGANISATION_NAME',
                       getattr(settings, 'OFFICE_NAME', 'EasyOffice'))
        _from = getattr(settings, 'DEFAULT_FROM_EMAIL',
                        f'noreply@{_org.lower().replace(" ", "")}.org')
        _reason_html = (
            f'<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;'
            f'padding:12px 16px;font-size:14px;color:#991b1b;margin:14px 0">'
            f'<strong>Reason:</strong> {signer.decline_reason}</div>'
            if signer.decline_reason else ''
        )
        _detail_url = base_url.rstrip('/') + f'/files/signatures/{sig_req.pk}/'
        _html = (
            f'<!DOCTYPE html><html><head><meta charset="UTF-8"/>'
            f'<style>body{{margin:0;padding:0;background:#f1f5f9;font-family:\'Segoe UI\',Arial,sans-serif;}} '
            f'.w{{max-width:580px;margin:28px auto;background:#fff;border-radius:16px;overflow:hidden;'
            f'box-shadow:0 4px 24px rgba(0,0,0,.08);}} '
            f'.hdr{{background:linear-gradient(135deg,#7f1d1d,#ef4444);padding:32px 36px;text-align:center;}} '
            f'.hdr h1{{margin:0;color:#fff;font-size:20px;font-weight:800;}} '
            f'.hdr p{{margin:6px 0 0;color:rgba(255,255,255,.75);font-size:13px;}} '
            f'.body{{padding:28px 36px;font-size:15px;color:#1e293b;line-height:1.7;}} '
            f'.btn{{display:inline-block;background:#3b82f6;color:#fff;padding:11px 26px;'
            f'border-radius:10px;text-decoration:none;font-weight:700;}} '
            f'.footer{{background:#f8fafc;padding:16px 36px;border-top:1px solid #e2e8f0;'
            f'text-align:center;font-size:12px;color:#94a3b8;}}</style></head>'
            f'<body><div class="w"><div class="hdr"><h1>❌ Signing Declined</h1>'
            f'<p>{_org}</p></div><div class="body">'
            f'<p>Dear <strong>{sig_req.created_by.full_name}</strong>,</p>'
            f'<p><strong>{signer.name}</strong> ({signer.email}) has <strong>declined</strong> '
            f'to sign <em>"{sig_req.title}"</em>.</p>{_reason_html}'
            f'<div style="text-align:center;margin:20px 0">'
            f'<a href="{_detail_url}" class="btn">View Signature Request →</a></div></div>'
            f'<div class="footer">{_org} · Document Signing System</div></div></body></html>'
        )
        msg = _EM(
            subject=f'Declined: {signer.name} declined to sign "{sig_req.title}"',
            body=_html,
            from_email=_from,
            to=[sig_req.created_by.email] if sig_req.created_by.email else [],
        )
        msg.content_subtype = 'html'
        msg.send()
    except Exception:
        logger.exception('Could not send decline email for %s', sig_req.pk)

    try:
        if hasattr(sig_req, 'update_status'):
            sig_req.update_status()
    except Exception:
        logger.exception('Could not update_status after decline for %s', sig_req.pk)


# ─────────────────────────────────────────────────────────────────────────────
# External (no-login) file sharing
# ─────────────────────────────────────────────────────────────────────────────

@_task
def send_external_share_invitation(share_id, base_url):
    """Send the initial 'you have a shared document' email to the recipient."""
    from apps.files.models import ExternalFileShare
    from apps.files.external_share_utils import send_invitation_email, write_audit

    try:
        share = ExternalFileShare.objects.select_related('file', 'created_by').get(pk=share_id)
    except ExternalFileShare.DoesNotExist:
        logger.warning('send_external_share_invitation: share %s gone', share_id)
        return

    if send_invitation_email(share, base_url):
        write_audit(share, 'email_sent',
                    actor=share.created_by,
                    notes=f'Invitation sent to {share.recipient_email}')


@_task
def send_external_share_device_verification(device_id, base_url):
    """Owner-side email asking accept/decline for a newly-seen device."""
    from apps.files.models import ExternalShareDevice
    from apps.files.external_share_utils import send_device_verification_email, write_audit

    try:
        device = ExternalShareDevice.objects.select_related(
            'share', 'share__file', 'share__created_by'
        ).get(pk=device_id)
    except ExternalShareDevice.DoesNotExist:
        return

    if send_device_verification_email(device, base_url):
        write_audit(device.share, 'device_pending',
                    device=device,
                    notes=f'Verification email sent to {device.share.created_by.email}')


@_task
def send_external_share_device_decision(device_id, base_url):
    """Recipient-side notification — 'your device was accepted/declined'."""
    from apps.files.models import ExternalShareDevice
    from apps.files.external_share_utils import send_device_decision_to_recipient

    try:
        device = ExternalShareDevice.objects.select_related(
            'share', 'share__file', 'share__created_by'
        ).get(pk=device_id)
    except ExternalShareDevice.DoesNotExist:
        return

    send_device_decision_to_recipient(device, base_url)