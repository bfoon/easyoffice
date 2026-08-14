"""
apps/inventory/license_notifications.py
───────────────────────────────────────
Licence event notifications. Same contract as
apps/inventory/notifications.py: two channels (in-app + email), called
from the services layer on transaction commit, and **never raises** —
a mail server outage must not roll back a renewal.

Who hears about what
════════════════════
Event                     In-app                          Email
──────────────────────────────────────────────────────────────────────────
license_created           Licence team                    —
seat_assigned             The person + licence team       The person
seat_released             Licence team                    —
license_expiring          Owner + licence team            Owner + managers
                                                          (+ customer, opt-in)
license_expired           Owner + licence team + CEO      Owner + managers
license_renewed           Owner + licence team            Owner
seats_exhausted           Licence team                    Managers

"Licence team" and "managers" are resolved from the same
InventoryAccessGrant table the screens use — operate-level and
manage-level on the `licenses` module respectively. If nobody holds a
grant yet (fresh install), it falls back to the legacy ICT/admin groups
so alerts are never silently dropped.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.urls import reverse
from django.utils.html import escape

from .access import Module
from .notifications import _create_inapp, _send_email, _site_url, _users_in_groups

logger = logging.getLogger(__name__)


# Legacy fallback buckets, used only when no grants exist yet.
GROUP_LICENSE = ('ict', 'it', 'systems', 'sysadmin', 'inventory')
GROUP_LICENSE_MANAGER = ('head of ict', 'supervisor', 'manager', 'admin',
                         'administrator', 'ceo')
GROUP_CEO = ('ceo',)


# ════════════════════════════════════════════════════════════════════════════
# Recipients
# ════════════════════════════════════════════════════════════════════════════

def _grant_holders(min_level: str):
    from .access import users_with_module_access
    try:
        return list(users_with_module_access(Module.LICENSES, min_level))
    except Exception:
        logger.exception('Could not resolve licence %s holders.', min_level)
        return []


def license_team(lic=None) -> list:
    """Operate-level and above — the people who administer licences."""
    people = _grant_holders('operate')
    if not people:
        people = list(_users_in_groups(GROUP_LICENSE))
    if lic is not None:
        if lic.owner_id:
            people.append(lic.owner)
        if lic.holder_user_id:
            people.append(lic.holder_user)
    return _dedupe(people)


def license_managers(lic=None) -> list:
    """Manage-level — the people who sign off a renewal."""
    people = _grant_holders('manage')
    if not people:
        people = list(_users_in_groups(GROUP_LICENSE_MANAGER))
    if lic is not None and lic.owner_id:
        people.append(lic.owner)
    return _dedupe(people)


def _dedupe(users) -> list:
    seen, out = set(), []
    for u in users:
        if not u or not getattr(u, 'pk', None) or u.pk in seen:
            continue
        if not getattr(u, 'is_active', True):
            continue
        seen.add(u.pk)
        out.append(u)
    return out


def _emails(users) -> list[str]:
    return [u.email for u in users if getattr(u, 'email', '')]


def _url(lic) -> str:
    try:
        return reverse('inventory:license_detail', args=[lic.pk])
    except Exception:
        return ''


def _full_url(lic) -> str:
    site, path = _site_url(), _url(lic)
    return f'{site}{path}' if site and path else path


# ════════════════════════════════════════════════════════════════════════════
# HTML mail — one small, self-contained template
# ════════════════════════════════════════════════════════════════════════════

def _send_rich_email(recipients: list[str], subject: str, *, heading: str,
                     lead: str, rows: list[tuple[str, str]], cta_url: str = '',
                     cta_label: str = 'Open the licence', accent: str = '#1d4ed8',
                     footer: str = ''):
    """
    Sends a text body with an HTML alternative. Falls back to the plain
    sender if anything goes wrong building the HTML.
    """
    recipients = [r for r in recipients if r]
    if not recipients:
        return

    text_rows = '\n'.join(f'  {k}: {v}' for k, v in rows)
    text_body = f'{heading}\n\n{lead}\n\n{text_rows}\n'
    if cta_url:
        text_body += f'\n{cta_label}: {cta_url}\n'
    if footer:
        text_body += f'\n{footer}\n'

    try:
        row_html = ''.join(
            f'<tr>'
            f'<td style="padding:6px 14px 6px 0;color:#64748b;font-size:13px;'
            f'white-space:nowrap">{escape(k)}</td>'
            f'<td style="padding:6px 0;color:#0f172a;font-size:13px;'
            f'font-weight:600">{escape(v)}</td></tr>'
            for k, v in rows
        )
        button = (
            f'<a href="{escape(cta_url)}" style="display:inline-block;'
            f'background:{accent};color:#ffffff;text-decoration:none;'
            f'padding:10px 18px;border-radius:8px;font-size:13px;'
            f'font-weight:700">{escape(cta_label)}</a>'
        ) if cta_url else ''

        html = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
            background:#f1f5f9;padding:24px">
  <div style="max-width:560px;margin:0 auto;background:#ffffff;border-radius:14px;
              overflow:hidden;border:1px solid #e2e8f0">
    <div style="background:{accent};padding:18px 22px">
      <div style="color:rgba(255,255,255,.72);font-size:11px;letter-spacing:.14em;
                  text-transform:uppercase;font-weight:700">EasyOffice · Licences</div>
      <div style="color:#ffffff;font-size:18px;font-weight:800;margin-top:4px">
        {escape(heading)}</div>
    </div>
    <div style="padding:20px 22px">
      <p style="margin:0 0 14px;color:#334155;font-size:14px;line-height:1.55">
        {escape(lead)}</p>
      <table style="border-collapse:collapse;width:100%">{row_html}</table>
      <div style="margin-top:20px">{button}</div>
    </div>
    <div style="padding:14px 22px;background:#f8fafc;border-top:1px solid #e2e8f0;
                color:#94a3b8;font-size:11px">
      {escape(footer or 'Sent automatically by the EasyOffice inventory module.')}
    </div>
  </div>
</div>"""

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
            to=recipients,
        )
        msg.attach_alternative(html, 'text/html')
        msg.send(fail_silently=True)
    except Exception:
        logger.exception('Rich licence email failed, falling back: %s', subject)
        _send_email(recipients, subject, text_body)


def _money_rows(lic) -> list[tuple[str, str]]:
    cur = lic.currency
    rows = [
        ('Seats', f'{lic.seats_assigned} of {lic.seats} in use'),
        ('Cost per seat', f'{cur} {lic.cost_per_seat:,}'),
        ('Total cost', f'{cur} {lic.total_cost:,} per term'),
    ]
    if lic.total_price:
        rows.append(('Selling price', f'{cur} {lic.total_price:,}'))
        rows.append(('Margin', f'{cur} {lic.margin:,}'))
    return rows


# ════════════════════════════════════════════════════════════════════════════
# Events
# ════════════════════════════════════════════════════════════════════════════

def notify_license_created(lic, *, actor=None):
    title = f'Licence added: {lic.name}'
    body = (f'{lic.reference} — {lic.seats} seat(s), '
            f'{"perpetual" if lic.is_perpetual else f"expires {lic.end_date}"}.')
    for u in license_team(lic):
        _create_inapp(u, title=title, body=body, url=_url(lic))


def notify_seat_assigned(seat, *, actor=None):
    lic = seat.license
    title = f'Licence assigned: {lic.name}'
    body = (f'{lic.name} ({lic.reference}) is now assigned to '
            f'{seat.holder_label}.')

    if seat.user_id:
        _create_inapp(seat.user, title=title, body=body, url=_url(lic))
    for u in license_team(lic):
        _create_inapp(u, title=title, body=body, url=_url(lic))

    if seat.email:
        rows = [('Licence', lic.name), ('Reference', lic.reference)]
        if not lic.is_perpetual and lic.end_date:
            rows.append(('Valid until', str(lic.end_date)))
        if lic.account_email:
            rows.append(('Account', lic.account_email))
        _send_rich_email(
            [seat.email],
            subject=f'[EasyOffice] {lic.name} is now assigned to you',
            heading='A licence was assigned to you',
            lead=(f'You have a seat on {lic.name}. Keep the credentials to '
                  f'yourself — the seat is tracked against your name.'),
            rows=rows,
            cta_url=_full_url(lic),
            cta_label='View the licence',
        )


def notify_seat_released(seat, *, actor=None):
    lic = seat.license
    title = f'Seat released: {lic.name}'
    body = (f'{seat.holder_label} released a seat on {lic.reference}. '
            f'{lic.seats_free} seat(s) now free.')
    for u in license_team(lic):
        _create_inapp(u, title=title, body=body, url=_url(lic))


def notify_license_expiring(lic, *, days_left: int):
    """The core alert. Fires once per threshold per term."""
    urgency = 'critical' if days_left <= 7 else 'soon'
    accent = '#be123c' if urgency == 'critical' else '#b45309'
    when = 'today' if days_left == 0 else f'in {days_left} day(s)'

    title = f'⏳ Licence expires {when}: {lic.name}'
    body = (f'{lic.reference} ends on {lic.end_date}. '
            f'{"Auto-renews with the vendor." if lic.auto_renew else "Renewal is not automatic."}')

    for u in license_team(lic):
        _create_inapp(u, title=title, body=body, url=_url(lic))

    rows = [
        ('Licence',    f'{lic.name} ({lic.reference})'),
        ('Held by',    lic.holder_label),
        ('Ends',       str(lic.end_date)),
        ('Auto-renew', 'Yes' if lic.auto_renew else 'No'),
    ]
    if lic.vendor_id:
        rows.append(('Vendor', lic.vendor.name))
    rows += _money_rows(lic)
    if lic.grace_days:
        rows.append(('Grace period', f'{lic.grace_days} day(s) after the end date'))

    lead = (f'{lic.name} expires {when}. '
            + ('The vendor will renew it automatically — check the budget is in place.'
               if lic.auto_renew else
               'Renew it or plan the switch-off before service stops.'))

    _send_rich_email(
        _emails(license_managers(lic)),
        subject=f'[EasyOffice] Licence expires {when} — {lic.name}',
        heading=f'Licence expires {when}',
        lead=lead,
        rows=rows,
        cta_url=_full_url(lic),
        cta_label='Renew or review',
        accent=accent,
    )

    # Optional copy to the customer this licence was bought for.
    if lic.notify_customer and lic.customer_email:
        _send_rich_email(
            [lic.customer_email],
            subject=f'[EasyOffice] Your {lic.name} licence expires {when}',
            heading=f'Your licence expires {when}',
            lead=(f'The {lic.name} licence we manage for {lic.customer_name} '
                  f'ends on {lic.end_date}. Let us know if you would like us '
                  f'to renew it.'),
            rows=[('Licence', lic.name),
                  ('Seats', str(lic.seats)),
                  ('Ends', str(lic.end_date))],
            accent=accent,
            footer='Easy Solutions — reply to this email to arrange a renewal.',
        )


def notify_license_expired(lic):
    title = f'⛔ Licence expired: {lic.name}'
    body = (f'{lic.reference} expired on {lic.end_date}.'
            + (f' Grace period runs to {lic.expiry_date}.' if lic.grace_days else ''))

    for u in _dedupe(license_team(lic) + list(_users_in_groups(GROUP_CEO))):
        _create_inapp(u, title=title, body=body, url=_url(lic))

    rows = [
        ('Licence', f'{lic.name} ({lic.reference})'),
        ('Held by', lic.holder_label),
        ('Expired', str(lic.end_date)),
        ('Seats affected', str(lic.seats_assigned)),
    ]
    if lic.grace_days:
        rows.append(('Service stops', str(lic.expiry_date)))
    rows += _money_rows(lic)

    _send_rich_email(
        _emails(license_managers(lic)),
        subject=f'[EasyOffice] Licence expired — {lic.name}',
        heading='Licence expired',
        lead=(f'{lic.name} passed its end date. '
              f'{lic.seats_assigned} seat(s) are affected. Renew it, or '
              f'release the seats so the register stays honest.'),
        rows=rows,
        cta_url=_full_url(lic),
        cta_label='Renew now',
        accent='#be123c',
    )


def notify_license_renewed(lic, renewal, *, actor=None):
    title = f'Licence renewed: {lic.name}'
    body = f'{lic.reference} now runs to {renewal.new_end}.'
    for u in license_team(lic):
        _create_inapp(u, title=title, body=body, url=_url(lic))

    rows = [
        ('Licence',   f'{lic.name} ({lic.reference})'),
        ('New end',   str(renewal.new_end)),
        ('Term',      f'{renewal.term_months} month(s)'),
        ('Seats',     str(renewal.seats)),
        ('Cost',      f'{renewal.currency} {renewal.total_cost:,}'),
    ]
    if renewal.total_price:
        rows.append(('Invoiced', f'{renewal.currency} {renewal.total_price:,}'))
    delta = renewal.cost_delta
    if delta:
        rows.append(('Change vs last term', f'{renewal.currency} {delta:,}'))

    _send_rich_email(
        _emails(_dedupe([lic.owner] if lic.owner_id else []) or license_managers(lic)),
        subject=f'[EasyOffice] Licence renewed — {lic.name}',
        heading='Licence renewed',
        lead=f'{lic.name} has been renewed through {renewal.new_end}.',
        rows=rows,
        cta_url=_full_url(lic),
        cta_label='View the licence',
        accent='#047857',
    )


def notify_seats_exhausted(lic):
    """Every seat is taken — someone is about to be told "no licence"."""
    title = f'No seats left: {lic.name}'
    body = (f'All {lic.seats} seat(s) on {lic.reference} are assigned. '
            f'Buy more seats before the next request.')
    for u in license_team(lic):
        _create_inapp(u, title=title, body=body, url=_url(lic))

    _send_rich_email(
        _emails(license_managers(lic)),
        subject=f'[EasyOffice] No seats left — {lic.name}',
        heading='Licence is fully allocated',
        lead=(f'Every seat on {lic.name} is in use. The next person who '
              f'asks will have to wait for a purchase.'),
        rows=[('Licence', f'{lic.name} ({lic.reference})'),
              ('Seats', f'{lic.seats_assigned} of {lic.seats}'),
              ('Cost of one more seat', f'{lic.currency} {lic.unit_cost:,}')],
        cta_url=_full_url(lic),
        cta_label='Review the licence',
        accent='#b45309',
    )
