"""
apps/files/external_share_utils.py
──────────────────────────────────
Helper utilities for the external (no-login) file-sharing feature.

SECURITY CHANGES vs previous version
  • Every user-controlled value interpolated into email HTML is now
    escaped (file names, recipient/sender names, browser/OS strings, …).
    Previously a file named  <img src=x onerror=…>.pdf  injected live
    HTML into both the invitation and the owner-verification emails.
  • get_client_ip now delegates to security_utils.client_ip, which only
    honours X-Forwarded-For when TRUSTED_PROXY_COUNT is configured.
    Spoofed XFF headers previously poisoned audit rows and device
    fingerprints.
"""
from __future__ import annotations

import hashlib
import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from apps.files.security_utils import client_ip as _trusted_client_ip

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Fingerprinting & UA parsing
# ─────────────────────────────────────────────────────────────────────────────

def get_client_ip(request):
    """Trusted-proxy-aware client IP. See security_utils.client_ip."""
    return _trusted_client_ip(request)


def _ip_subnet(ip):
    """
    Group IPv4 addresses by /24 so a phone bouncing between cell towers
    doesn't keep registering as a new device. IPv6 uses the /64 prefix.
    """
    if not ip:
        return ''
    if ':' in ip:
        # /64 keeps the same household together on IPv6.
        parts = ip.split(':')
        return ':'.join(parts[:4]) + '::/64'
    parts = ip.split('.')
    if len(parts) == 4:
        return '.'.join(parts[:3]) + '.0/24'
    return ip


def build_device_fingerprint(client_token, user_agent, ip):
    """
    Produce a stable SHA-256 hex digest from:
      • a client-side fingerprint token (set on first visit)
      • the User-Agent string
      • the subnet of the IP

    The client_token dominates: as long as the recipient stays on the same
    browser profile, the fingerprint never changes even if their IP moves
    or their UA gets a minor patch bump.
    """
    parts = [
        (client_token or '').strip(),
        (user_agent or '').strip(),
        _ip_subnet(ip),
    ]
    raw = '||'.join(parts).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


# ── UA parsing ─────────────────────────────────────────────────────────────

try:
    from user_agents import parse as _ua_parse  # type: ignore
    _UA_LIB_AVAILABLE = True
except Exception:  # pragma: no cover
    _ua_parse = None
    _UA_LIB_AVAILABLE = False


def _parse_ua_fallback(ua):
    """Tiny fallback parser used when the user_agents lib is unavailable."""
    if not ua:
        return {'browser': '', 'os': '', 'device_type': 'unknown'}

    ua_lower = ua.lower()

    if 'edg/' in ua_lower:
        browser = 'Edge'
    elif 'opr/' in ua_lower or 'opera' in ua_lower:
        browser = 'Opera'
    elif 'firefox/' in ua_lower:
        browser = 'Firefox'
    elif 'chrome/' in ua_lower and 'safari/' in ua_lower:
        browser = 'Chrome'
    elif 'safari/' in ua_lower:
        browser = 'Safari'
    else:
        browser = 'Unknown'

    if 'windows nt 10' in ua_lower:
        os_name = 'Windows 10/11'
    elif 'windows' in ua_lower:
        os_name = 'Windows'
    elif 'iphone' in ua_lower or 'ipad' in ua_lower or 'ios ' in ua_lower:
        os_name = 'iOS'
    elif 'mac os x' in ua_lower or 'macintosh' in ua_lower:
        os_name = 'macOS'
    elif 'android' in ua_lower:
        os_name = 'Android'
    elif 'linux' in ua_lower:
        os_name = 'Linux'
    else:
        os_name = 'Unknown'

    if 'mobile' in ua_lower or 'iphone' in ua_lower:
        device_type = 'mobile'
    elif 'tablet' in ua_lower or 'ipad' in ua_lower:
        device_type = 'tablet'
    else:
        device_type = 'desktop'

    return {'browser': browser, 'os': os_name, 'device_type': device_type}


def parse_user_agent(ua):
    """Returns {'browser': str, 'os': str, 'device_type': str}."""
    if not ua:
        return {'browser': '', 'os': '', 'device_type': 'unknown'}

    if _UA_LIB_AVAILABLE:
        try:
            parsed = _ua_parse(ua)
            if parsed.is_mobile:
                device_type = 'mobile'
            elif parsed.is_tablet:
                device_type = 'tablet'
            elif parsed.is_pc:
                device_type = 'desktop'
            elif parsed.is_bot:
                device_type = 'bot'
            else:
                device_type = 'unknown'

            browser = (parsed.browser.family or '').strip()
            os_name = (parsed.os.family or '').strip()
            if parsed.os.version_string:
                os_name = f'{os_name} {parsed.os.version_string}'.strip()
            return {
                'browser': browser[:60],
                'os': os_name[:60],
                'device_type': device_type,
            }
        except Exception:
            logger.exception('user_agents parse failed for: %r', ua[:120])

    return _parse_ua_fallback(ua)


# ─────────────────────────────────────────────────────────────────────────────
# Email helpers
# ─────────────────────────────────────────────────────────────────────────────

def _from_email():
    return getattr(
        settings, 'DEFAULT_FROM_EMAIL',
        f'noreply@{getattr(settings, "ORGANISATION_NAME", "easyoffice").lower().replace(" ", "")}.org',
    )


def _org_name():
    return getattr(settings, 'ORGANISATION_NAME',
                   getattr(settings, 'OFFICE_NAME', 'EasyOffice'))


def _abs_url(base_url, path):
    """Join base_url + path safely, stripping double slashes."""
    if not path:
        return base_url.rstrip('/')
    return base_url.rstrip('/') + '/' + path.lstrip('/')


def send_invitation_email(share, base_url):
    """
    Email the external recipient with the "Open File" link.
    All interpolated values are HTML-escaped.
    """
    try:
        recipient_email = share.recipient_email  # decrypts
        recipient_name  = share.recipient_name or 'there'
        if not recipient_email:
            logger.warning('Skipping invitation email: no recipient on share %s', share.pk)
            return False

        open_url    = _abs_url(base_url, share.open_url)
        org         = escape(_org_name())
        sender_name = escape(share.created_by.full_name if share.created_by_id else _org_name())
        sender_email = escape(share.created_by.email if share.created_by_id else '')
        file_name   = escape(share.file.name)
        recipient_name_html  = escape(recipient_name)
        recipient_email_html = escape(recipient_email)

        message_block = ''
        if share.message:
            safe_msg = escape(share.message).replace('\n', '<br>')
            message_block = (
                f'<div style="background:#f8fafc;border-left:4px solid #3b82f6;'
                f'padding:14px 18px;border-radius:8px;font-size:14px;color:#334155;'
                f'margin:18px 0;line-height:1.6"><strong>Message from '
                f'{sender_name}:</strong><br>{safe_msg}</div>'
            )

        expiry_human = share.expires_at.strftime('%b %d, %Y · %H:%M UTC')

        html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"/>
<style>
body{{margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;}}
.w{{max-width:580px;margin:28px auto;background:#fff;border-radius:16px;
   overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);}}
.hdr{{background:linear-gradient(135deg,#1e3a8a,#3b82f6);padding:32px 36px;text-align:center;}}
.hdr h1{{margin:0;color:#fff;font-size:22px;font-weight:800;}}
.hdr p{{margin:6px 0 0;color:rgba(255,255,255,.78);font-size:13px;}}
.body{{padding:28px 36px;font-size:15px;color:#1e293b;line-height:1.7;}}
.btn{{display:inline-block;background:#3b82f6;color:#fff;padding:12px 28px;
     border-radius:10px;text-decoration:none;font-weight:700;font-size:15px;}}
.meta{{margin:18px 0;padding:14px 16px;background:#f1f5f9;border-radius:10px;
      font-size:13px;color:#475569;}}
.meta strong{{color:#1e293b;}}
.warn{{margin-top:20px;padding:12px 16px;background:#fef3c7;border:1px solid #fcd34d;
      border-radius:10px;font-size:12px;color:#78350f;line-height:1.6;}}
.footer{{background:#f8fafc;padding:16px 36px;border-top:1px solid #e2e8f0;
        text-align:center;font-size:12px;color:#94a3b8;}}
</style></head><body>
<div class="w">
  <div class="hdr">
    <h1>📄 You have a shared document</h1>
    <p>{org}</p>
  </div>
  <div class="body">
    <p>Hi <strong>{recipient_name_html}</strong>,</p>
    <p><strong>{sender_name}</strong> has shared a document with you:</p>
    <p style="font-size:18px;font-weight:700;color:#1e3a8a;margin:8px 0">"{file_name}"</p>
    {message_block}
    <div style="text-align:center;margin:24px 0">
      <a href="{open_url}" class="btn">Open File →</a>
    </div>
    <div class="meta">
      <div>🕒 <strong>Link expires:</strong> {expiry_human}</div>
      <div>🔐 <strong>Permission:</strong> {escape(share.get_permission_display())}</div>
      <div>📥 <strong>Downloads allowed:</strong> {share.max_downloads or 'Unlimited'}</div>
    </div>
    <div class="warn">
      ⚠️ <strong>Heads-up:</strong> The first time you open this from a new
      device, {sender_name} will be asked to approve that device before you
      can download. Please use the same browser to avoid repeated checks.
    </div>
  </div>
  <div class="footer">
    {org} · External File Share<br>
    Sent to {recipient_email_html} on behalf of {sender_email or sender_name}
  </div>
</div>
</body></html>"""

        msg = EmailMessage(
            subject=f'{share.created_by.full_name if share.created_by_id else _org_name()} '
                    f'shared "{share.file.name}" with you',
            body=html,
            from_email=_from_email(),
            to=[recipient_email],
        )
        msg.content_subtype = 'html'
        msg.send(fail_silently=True)
        return True
    except Exception:
        logger.exception('Could not send external share invitation for %s', share.pk)
        return False


def send_device_verification_email(device, base_url):
    """
    Email the share's owner asking them to ACCEPT or DECLINE this newly-seen
    device. All interpolated values are HTML-escaped.
    """
    try:
        share = device.share
        owner = share.created_by
        if not owner or not owner.email:
            logger.warning('No owner email for device verification on share %s', share.pk)
            return False

        recipient_email = escape(share.recipient_email)
        accept_url  = _abs_url(base_url, reverse('external_share_device_decide',
                                                  kwargs={'token': device.verify_token,
                                                          'decision': 'accept'}))
        decline_url = _abs_url(base_url, reverse('external_share_device_decide',
                                                  kwargs={'token': device.verify_token,
                                                          'decision': 'decline'}))
        manage_url  = _abs_url(base_url, reverse('external_share_manage',
                                                  kwargs={'pk': share.pk}))

        org        = escape(_org_name())
        owner_name = escape(owner.full_name)
        file_name  = escape(share.file.name)

        device_table = f"""
<table style="width:100%;border-collapse:collapse;font-size:13px;color:#334155;
              background:#f8fafc;border-radius:10px;overflow:hidden;margin:12px 0">
  <tr><td style="padding:8px 14px;font-weight:600;width:130px">Recipient</td>
      <td style="padding:8px 14px">{recipient_email}</td></tr>
  <tr><td style="padding:8px 14px;font-weight:600;background:#fff">IP address</td>
      <td style="padding:8px 14px;background:#fff">{escape(device.ip_address or '—')}</td></tr>
  <tr><td style="padding:8px 14px;font-weight:600">Browser</td>
      <td style="padding:8px 14px">{escape(device.browser_name or 'Unknown')}</td></tr>
  <tr><td style="padding:8px 14px;font-weight:600;background:#fff">Operating system</td>
      <td style="padding:8px 14px;background:#fff">{escape(device.os_name or 'Unknown')}</td></tr>
  <tr><td style="padding:8px 14px;font-weight:600">Device type</td>
      <td style="padding:8px 14px">{escape((device.device_type or 'unknown').title())}</td></tr>
  <tr><td style="padding:8px 14px;font-weight:600;background:#fff">Location (approx)</td>
      <td style="padding:8px 14px;background:#fff">{escape(device.city or '—')} {escape(device.country_code or '')}</td></tr>
  <tr><td style="padding:8px 14px;font-weight:600">First seen</td>
      <td style="padding:8px 14px">{timezone.localtime(device.first_seen_at).strftime('%b %d, %Y · %H:%M')}</td></tr>
</table>"""

        html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"/>
<style>
body{{margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;}}
.w{{max-width:600px;margin:28px auto;background:#fff;border-radius:16px;
   overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08);}}
.hdr{{background:linear-gradient(135deg,#92400e,#f59e0b);padding:30px 36px;text-align:center;}}
.hdr h1{{margin:0;color:#fff;font-size:20px;font-weight:800;}}
.hdr p{{margin:6px 0 0;color:rgba(255,255,255,.85);font-size:13px;}}
.body{{padding:28px 36px;font-size:15px;color:#1e293b;line-height:1.7;}}
.btn{{display:inline-block;padding:11px 24px;border-radius:10px;
     text-decoration:none;font-weight:700;font-size:14px;margin:0 6px;}}
.btn-ok{{background:#10b981;color:#fff;}}
.btn-no{{background:#ef4444;color:#fff;}}
.footer{{background:#f8fafc;padding:16px 36px;border-top:1px solid #e2e8f0;
        text-align:center;font-size:12px;color:#94a3b8;}}
</style></head><body>
<div class="w">
  <div class="hdr">
    <h1>🔐 New device wants to open your share</h1>
    <p>{org} · External File Share</p>
  </div>
  <div class="body">
    <p>Hi <strong>{owner_name}</strong>,</p>
    <p>Someone is trying to open the file <em>"{file_name}"</em> for the
    first time from a device we haven't seen before. Please verify whether
    this is the recipient you expected.</p>
    {device_table}
    <div style="text-align:center;margin:22px 0">
      <a href="{accept_url}"  class="btn btn-ok">✓ Review &amp; accept device</a>
      <a href="{decline_url}" class="btn btn-no">✗ Review &amp; decline device</a>
    </div>
    <p style="font-size:13px;color:#64748b;text-align:center">
      You'll be asked to confirm before the decision is applied.<br>
      Or <a href="{manage_url}" style="color:#3b82f6">manage all devices for this share</a>.
    </p>
  </div>
  <div class="footer">
    {org} · You're receiving this because you shared a file externally.
  </div>
</div>
</body></html>"""

        msg = EmailMessage(
            subject=f'New device requesting access to "{share.file.name}"',
            body=html,
            from_email=_from_email(),
            to=[owner.email],
        )
        msg.content_subtype = 'html'
        msg.send(fail_silently=True)
        return True
    except Exception:
        logger.exception('Could not send device verification email for device %s', device.pk)
        return False


def send_device_decision_to_recipient(device, base_url):
    """
    Tell the recipient that their device was approved (or declined).
    All interpolated values are HTML-escaped.
    """
    try:
        share = device.share
        recipient_email = share.recipient_email
        if not recipient_email:
            return False

        accepted   = device.is_accepted
        org        = escape(_org_name())
        owner_name = escape(share.created_by.full_name)
        file_name  = escape(share.file.name)
        open_url   = _abs_url(base_url, share.open_url)

        if accepted:
            subject = f'Your access to "{share.file.name}" is approved'
            colour  = '#10b981'
            heading = '✅ Device approved'
            body_p  = (f'Your device has been approved by <strong>{owner_name}</strong>. '
                       f'You can now open <em>"{file_name}"</em>.')
            cta     = (f'<a href="{open_url}" style="display:inline-block;background:#10b981;'
                       f'color:#fff;padding:12px 28px;border-radius:10px;text-decoration:none;'
                       f'font-weight:700">Open File →</a>')
        else:
            subject = f'Your access to "{share.file.name}" was declined'
            colour  = '#ef4444'
            heading = '❌ Device declined'
            body_p  = (f'Unfortunately, <strong>{owner_name}</strong> declined this '
                       f'device for the file <em>"{file_name}"</em>. If you believe this is in '
                       f'error, please contact them directly.')
            cta     = ''

        html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"/></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif">
<div style="max-width:560px;margin:28px auto;background:#fff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,.08)">
  <div style="background:{colour};padding:28px 36px;text-align:center;color:#fff">
    <h1 style="margin:0;font-size:20px;font-weight:800">{heading}</h1>
    <p style="margin:6px 0 0;color:rgba(255,255,255,.85);font-size:13px">{org}</p>
  </div>
  <div style="padding:28px 36px;font-size:15px;color:#1e293b;line-height:1.7">
    <p>{body_p}</p>
    <div style="text-align:center;margin:20px 0">{cta}</div>
  </div>
  <div style="background:#f8fafc;padding:16px 36px;border-top:1px solid #e2e8f0;text-align:center;font-size:12px;color:#94a3b8">{org} · External File Share</div>
</div>
</body></html>"""

        msg = EmailMessage(
            subject=subject,
            body=html,
            from_email=_from_email(),
            to=[recipient_email],
        )
        msg.content_subtype = 'html'
        msg.send(fail_silently=True)
        return True
    except Exception:
        logger.exception('Could not send decision email for device %s', device.pk)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Audit helper
# ─────────────────────────────────────────────────────────────────────────────

def write_audit(share, action, *, device=None, actor=None, notes='', ip_address=None):
    """Single entry point for writing an ExternalShareAuditEvent row."""
    try:
        from apps.files.models import ExternalShareAuditEvent
        ExternalShareAuditEvent.objects.create(
            share=share,
            device=device,
            action=action,
            actor=actor,
            notes=(notes or '')[:2000],
            ip_address=ip_address,
        )
    except Exception:
        logger.exception('Could not write external share audit (action=%s, share=%s)',
                         action, getattr(share, 'pk', None))