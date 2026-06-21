"""
apps/mobile_api/push.py
───────────────────────
Firebase Cloud Messaging sender for the EasyOffice mobile app.

Reads the service-account key from the FIREBASE_CREDENTIALS env var,
initializes the Firebase Admin SDK once, and exposes send_push_to_user()
which pushes a notification to every device a user has registered.

Dead tokens (UNREGISTERED / invalid) are pruned automatically.
"""

import logging
import os
import threading

log = logging.getLogger(__name__)

_init_lock = threading.Lock()
_initialized = False
_firebase_app = None


def _ensure_initialized():
    """Initialize the Firebase Admin SDK once, lazily and thread-safely."""
    global _initialized, _firebase_app
    if _initialized:
        return _firebase_app

    with _init_lock:
        if _initialized:
            return _firebase_app
        try:
            import firebase_admin
            from firebase_admin import credentials

            cred_path = os.environ.get('FIREBASE_CREDENTIALS')
            if not cred_path or not os.path.exists(cred_path):
                log.warning(
                    'push: FIREBASE_CREDENTIALS not set or file missing (%s); '
                    'push disabled.', cred_path,
                )
                _initialized = True  # don't retry every call
                return None

            cred = credentials.Certificate(cred_path)
            # Guard against double-init if another import path already did it.
            try:
                _firebase_app = firebase_admin.get_app()
            except ValueError:
                _firebase_app = firebase_admin.initialize_app(cred)

            log.info('push: Firebase Admin SDK initialized.')
        except Exception:
            log.exception('push: Firebase init failed; push disabled.')
            _firebase_app = None
        finally:
            _initialized = True

    return _firebase_app


def send_push_to_user(user, title, body, data=None):
    """
    Send a notification to all of `user`'s registered devices.

    `data` is an optional dict of string key/values delivered alongside the
    notification (e.g. room_id) so the app can deep-link on tap.

    Returns the number of devices successfully sent to. Best-effort: any
    failure is logged, never raised.
    """
    app = _ensure_initialized()
    if app is None:
        return 0

    try:
        from firebase_admin import messaging
        from apps.messaging.models import DeviceToken
    except Exception:
        log.exception('push: import failed')
        return 0

    tokens = list(
        DeviceToken.objects.filter(user=user).values_list('token', flat=True)
    )
    if not tokens:
        return 0

    # Normalize data values to strings (FCM requires string values).
    str_data = {str(k): str(v) for k, v in (data or {}).items()}

    sent = 0
    dead_tokens = []

    for token in tokens:
        try:
            message = messaging.Message(
                token=token,
                notification=messaging.Notification(title=title, body=body),
                data=str_data,
                android=messaging.AndroidConfig(
                    priority='high',
                    notification=messaging.AndroidNotification(
                        sound='default',
                        channel_id='easyoffice_messages',
                    ),
                ),
            )
            messaging.send(message)
            sent += 1
        except Exception as e:
            # Token is dead/unregistered → mark for cleanup.
            msg = str(e).lower()
            if 'not found' in msg or 'unregistered' in msg or 'invalid' in msg:
                dead_tokens.append(token)
            else:
                log.exception('push: send failed for one token')

    # Prune dead tokens so we don't keep trying them.
    if dead_tokens:
        try:
            DeviceToken.objects.filter(token__in=dead_tokens).delete()
            log.info('push: pruned %d dead token(s).', len(dead_tokens))
        except Exception:
            log.exception('push: dead-token cleanup failed')

    return sent