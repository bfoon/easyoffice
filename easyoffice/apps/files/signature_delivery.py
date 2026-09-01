"""
apps/files/signature_delivery.py
────────────────────────────────
What happens to the signed PDF once every signer has signed.

Before this module the signed copy was created with ``uploaded_by`` set to
the *original document owner* and no share rows at all. A signer who was not
the owner therefore had no permission on the file they had just signed:
``_file_permission_for`` returned ``None`` and ``FileDownloadView`` raised
``Http404``. The signer saw "page not found" on their own signed document.

Delivery now has three legs, and this module owns all three:

  1. **File system.** Every participant with an EasyOffice account gets a
     ``FileShareAccess`` row on the signed file, so it appears in their File
     Manager and downloads normally. No duplicate copies are stored — one
     file, several grants.

  2. **In-app message.** Each signer gets a notification carrying a download
     action for the signed copy, so they can get to it without hunting
     through the file list.

  3. **Email.** Unchanged — ``_send_completion_email`` still sends the
     attachment and the 30-day public link.

Everything here is idempotent. ``deliver_signed_copies`` records a marker in
``SignatureRequest.metadata`` so a retried background task re-grants access
(cheap, and self-healing) without sending a second round of notifications.

Chat delivery
─────────────
Leg 2 also posts the signed PDF into each participant's direct conversation
with the sender, as a real file message carrying a ``linked_file`` reference
to the SharedFile. That is handled by
``apps.messaging.services.deliver_signed_document``, which is the default
handler — no settings change required.

To point it somewhere else, or to switch it off:

    # settings.py
    SIGNATURE_CHAT_DELIVERY = 'apps.other.module.my_handler'   # custom
    SIGNATURE_CHAT_DELIVERY = None                             # disabled

The handler is called once per recipient with keyword arguments only:

    handler(recipient=..., sender=..., signed_file=...,
            signature_request=..., download_url=...)

Any exception it raises is logged and swallowed — chat delivery must never
break the signing flow, and a missing messaging app is not an error.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)

DELIVERY_FLAG = 'signed_copy_delivered_at'

# Overridable via settings.SIGNATURE_CHAT_DELIVERY. Set that to None to turn
# chat delivery off and leave the file-system + notification legs running.
DEFAULT_CHAT_HANDLER = 'apps.messaging.services.deliver_signed_document'


# ── Who is entitled to the signed copy ──────────────────────────────────────

def signature_participants(sig_req, *, signed_only: bool = True):
    """
    Every EasyOffice user attached to a request: creator, signers, CC and
    viewers. External signers (no ``user``) are excluded — they receive the
    document by email and through their token link, which is the only access
    they can have without an account.

    Returns a list of ``(user, role)`` tuples, de-duplicated, creator first.
    """
    seen = set()
    out = []

    def add(user, role):
        if not user or not getattr(user, 'pk', None) or user.pk in seen:
            return
        seen.add(user.pk)
        out.append((user, role))

    add(getattr(sig_req, 'created_by', None), 'creator')

    signers = sig_req.signers.select_related('user')
    if signed_only:
        signers = signers.filter(status='signed')
    for signer in signers:
        add(signer.user, 'signer')

    try:
        for cc in sig_req.cc_recipients.select_related('user'):
            add(cc.user, cc.role or 'cc')
    except Exception:
        logger.exception('Could not read CC recipients for %s', sig_req.pk)

    return out


# ── Leg 1: put the file in their file system ────────────────────────────────

def grant_signed_file_access(sig_req, signed_file=None, *, permission='view'):
    """
    Give every participant a share row on the signed document.

    Never downgrades: a user who already holds 'edit' or 'full' keeps it.
    Returns the number of users who gained or kept access.
    """
    from apps.files.models import FileShareAccess

    signed_file = signed_file or sig_req.document
    if not signed_file:
        return 0

    granted = 0
    rank = {'view': 1, 'edit': 2, 'full': 3}

    for user, _role in signature_participants(sig_req):
        if user.pk == signed_file.uploaded_by_id:
            granted += 1          # owner already has full access
            continue
        try:
            access, created = FileShareAccess.objects.get_or_create(
                file=signed_file,
                user=user,
                defaults={
                    'permission': permission,
                    'granted_by': sig_req.created_by,
                },
            )
            if not created and rank.get(access.permission, 0) < rank.get(permission, 0):
                access.permission = permission
                access.save(update_fields=['permission', 'updated_at'])
            granted += 1
        except Exception:
            logger.exception(
                'Could not grant %s access to %s on %s',
                permission, user.pk, signed_file.pk,
            )

    return granted


# ── Leg 2: in-app message with the download action ──────────────────────────

def _download_url(signed_file, base_url=''):
    try:
        path = reverse('file_download', kwargs={'pk': signed_file.pk})
    except Exception:
        path = f'/files/{signed_file.pk}/download/'
    return (base_url.rstrip('/') + path) if base_url else path


def _notify_copy_delivered(sig_req, signed_file, user, role, base_url=''):
    from apps.files import views as files_views

    download = _download_url(signed_file, base_url)
    try:
        preview = reverse('file_preview', kwargs={'pk': signed_file.pk})
    except Exception:
        preview = ''

    files_views._notify_user(
        user=user,
        notif_type='sign_completed',
        title=f'Signed copy ready: {sig_req.title}',
        body=(
            f'{signed_file.name} has been added to your files. '
            f'All {sig_req.signers.count()} signer(s) have signed.'
        ),
        link=f'/files/signatures/{sig_req.pk}/',
        sender=sig_req.created_by,
        icon='bi-patch-check-fill',
        color='#10b981',
        actions=[
            {
                'label': 'Download',
                'url': download,
                'style': 'primary',
                'icon': 'bi-download',
            },
            {
                'label': 'Open in Files',
                'url': preview or '/files/',
                'style': 'secondary',
                'icon': 'bi-folder2-open',
            },
        ],
        extra_data={
            'request_id': str(sig_req.pk),
            'file_id': str(signed_file.pk),
            'file_name': signed_file.name,
            'role': role,
        },
    )


# ── Leg 3: optional chat/message-app attachment ─────────────────────────────

def _deliver_to_chat(sig_req, signed_file, user, base_url=''):
    path = getattr(settings, 'SIGNATURE_CHAT_DELIVERY', DEFAULT_CHAT_HANDLER)
    if not path:
        return False
    try:
        handler = import_string(path) if isinstance(path, str) else path
        handler(
            recipient=user,
            sender=sig_req.created_by,
            signed_file=signed_file,
            signature_request=sig_req,
            download_url=_download_url(signed_file, base_url),
        )
        return True
    except ImportError:
        # No messaging app installed, or the handler was renamed. Not fatal:
        # the file is already in their drive and the notification was sent.
        logger.info('Chat delivery handler %s unavailable; skipping', path)
        return False
    except Exception:
        logger.exception(
            'Chat delivery handler %s failed for user %s on %s',
            path, getattr(user, 'pk', None), sig_req.pk,
        )
        return False


# ── Orchestration ───────────────────────────────────────────────────────────

def deliver_signed_copies(sig_req, base_url='', *, notify=True):
    """
    Run all delivery legs for a completed request.

    Access is (re-)granted on every call — it is cheap and makes the function
    safe to run over historical requests. Notifications are sent only once,
    guarded by a marker in ``sig_req.metadata``.

    Returns a dict summarising what happened, useful for the backfill command.
    """
    signed_file = sig_req.document
    if not signed_file:
        return {'granted': 0, 'notified': 0, 'chat': 0, 'skipped': 'no document'}

    granted = grant_signed_file_access(sig_req, signed_file)

    try:
        from apps.files import views as files_views
        files_views._log_file_history(
            signed_file, 'shared', actor=sig_req.created_by,
            notes=f'Signed copy shared with {granted} participant(s)',
        )
    except Exception:
        pass

    metadata = sig_req.metadata or {}
    already_sent = bool(metadata.get(DELIVERY_FLAG))

    notified = chat_sent = 0
    if notify and not already_sent:
        for user, role in signature_participants(sig_req):
            # The creator owns the file and already gets the
            # "all signatures collected" notification from the task.
            if role == 'creator':
                continue
            try:
                _notify_copy_delivered(sig_req, signed_file, user, role, base_url)
                notified += 1
            except Exception:
                logger.exception(
                    'Could not notify %s about signed copy of %s', user.pk, sig_req.pk
                )
            if _deliver_to_chat(sig_req, signed_file, user, base_url):
                chat_sent += 1

        try:
            metadata[DELIVERY_FLAG] = timezone.now().isoformat()
            sig_req.metadata = metadata
            sig_req.save(update_fields=['metadata', 'updated_at'])
        except Exception:
            logger.exception('Could not record delivery marker for %s', sig_req.pk)

    return {
        'granted': granted,
        'notified': notified,
        'chat': chat_sent,
        'already_sent': already_sent,
    }
