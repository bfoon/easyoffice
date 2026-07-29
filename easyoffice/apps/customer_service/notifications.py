"""
apps/customer_service/notifications.py
──────────────────────────────────────
Single source of truth for customer-service notifications.

Two channels per event:
    • in-app : platform Notification rows (auto-discovered at runtime; falls
               back silently if the project's Notification model isn't found)
    • email  : django.core.mail.EmailMessage with a rendered HTML body

Each public function is named `notify_<event>` and takes the ticket/assignment
plus optional `actor` / `extra` kwargs. Wrap *every* call in a try/except at
the call-site (or call from a `transaction.on_commit` lambda) — notifications
should never break business logic.

Why a runtime-discovered Notification model?
    Different projects spell it differently — we try a few common locations.
    If none match, in-app delivery becomes a no-op and we still send email.
"""
from __future__ import annotations

import logging
from typing import Iterable, List, Optional, Sequence

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse, NoReverseMatch
from django.utils import timezone

logger = logging.getLogger(__name__)
User = get_user_model()


# ── In-app Notification model discovery ────────────────────────────────────
#
# Tries a sequence of likely import paths. First one that loads wins.
# Add yours to this list if you keep notifications somewhere else.

_NOTIFICATION_MODEL = None
_NOTIFICATION_TRIED = False


def _get_notification_model():
    global _NOTIFICATION_MODEL, _NOTIFICATION_TRIED
    if _NOTIFICATION_TRIED:
        return _NOTIFICATION_MODEL
    _NOTIFICATION_TRIED = True

    candidates = (
        ('apps.core.models',          'CoreNotification'),   # ← this project
        ('apps.notifications.models', 'Notification'),
        ('apps.core.models',          'Notification'),
        ('apps.accounts.models',      'Notification'),
        ('notifications.models',      'Notification'),
    )
    for module_path, name in candidates:
        try:
            mod = __import__(module_path, fromlist=[name])
            _NOTIFICATION_MODEL = getattr(mod, name)
            logger.info('customer_service.notifications: using %s.%s', module_path, name)
            return _NOTIFICATION_MODEL
        except Exception:
            continue
    logger.warning(
        'customer_service.notifications: no Notification model found; '
        'in-app delivery disabled.'
    )
    return None


# ── Helpers ────────────────────────────────────────────────────────────────

def _absolute_url(path: str) -> str:
    """
    Build an absolute URL for use in emails. Email clients will not follow a
    relative href, so we MUST return something fully qualified.

    Resolution order:
        1. settings.SITE_URL              (e.g. "https://app.example.com")
        2. settings.DEFAULT_DOMAIN        (e.g. "app.example.com")
        3. django.contrib.sites current Site.domain
        4. settings.ALLOWED_HOSTS[0]      (last-ditch)

    Whatever we land on, the result always starts with a scheme and has
    no trailing slash before the path.
    """
    if not path:
        path = '/'
    if not path.startswith('/'):
        path = '/' + path

    base = (getattr(settings, 'SITE_URL', '') or '').strip()

    if not base:
        domain = (getattr(settings, 'DEFAULT_DOMAIN', '') or '').strip()
        if not domain:
            try:
                from django.contrib.sites.models import Site
                domain = Site.objects.get_current().domain
            except Exception:
                domain = ''
        if not domain:
            hosts = list(getattr(settings, 'ALLOWED_HOSTS', []) or [])
            hosts = [h for h in hosts if h and h not in ('*', '.localhost')]
            if hosts:
                domain = hosts[0]
        if domain:
            scheme = 'http' if domain.startswith(('localhost', '127.', '0.0.0.0')) else 'https'
            base = f'{scheme}://{domain}'

    if not base:
        # Absolute last resort — never return a relative path to an email body.
        base = 'http://localhost:8000'

    if not base.startswith(('http://', 'https://')):
        base = 'https://' + base

    return base.rstrip('/') + path


def _ticket_url(ticket) -> str:
    if not ticket or not getattr(ticket, 'pk', None):
        return _absolute_url('/')
    try:
        path = reverse('customer_service_ticket_detail', kwargs={'pk': ticket.pk})
    except NoReverseMatch:
        # Fallback: at least take them to the CS dashboard so the email
        # button is never a dead link.
        try:
            path = reverse('customer_service_dashboard')
        except NoReverseMatch:
            path = '/'
    return _absolute_url(path)


def _from_email() -> str:
    return (
        getattr(settings, 'DEFAULT_FROM_EMAIL', None)
        or getattr(settings, 'SERVER_EMAIL', None)
        or 'no-reply@example.com'
    )


def _user_full_name(user) -> str:
    if not user:
        return 'System'
    return (
        getattr(user, 'full_name', None)
        or user.get_full_name()
        or user.username
    )


def _user_groups_lower(user) -> set:
    if not user or not getattr(user, 'is_authenticated', False):
        return set()
    return {g.lower() for g in user.groups.values_list('name', flat=True)}


def _users_in_department(department) -> List:
    """
    Every active user attached to a department through StaffProfile.
    Returns an empty list if Department or StaffProfile aren't wired up.
    """
    if not department:
        return []
    try:
        # StaffProfile lives in apps.organization in this project
        from apps.organization.models import StaffProfile
    except Exception:
        return []
    return list(
        User.objects
        .filter(is_active=True, staffprofile__department=department)
        .distinct()
    )


def _heads_for(*group_keywords: str, position_keywords: Sequence[str] = ()) -> List:
    """
    Find users who are 'heads' — by Django group name or by StaffProfile
    position title. Used for Head of CS, Head of Sales, etc.
    """
    matched = set()

    # 1. Django groups
    if group_keywords:
        from django.db.models import Q
        q = Q()
        for kw in group_keywords:
            q |= Q(groups__name__iexact=kw)
        matched.update(User.objects.filter(is_active=True).filter(q).distinct())

    # 2. Position title via StaffProfile
    if position_keywords:
        try:
            from apps.organization.models import StaffProfile
            from django.db.models import Q
            q = Q()
            for kw in position_keywords:
                q |= Q(staffprofile__position__title__icontains=kw)
            matched.update(User.objects.filter(is_active=True).filter(q).distinct())
        except Exception:
            pass

    return list(matched)


def heads_of_customer_service() -> List:
    return _heads_for(
        'Head of Customer Service', 'Head of CS', 'CS Head',
        position_keywords=('head of customer service', 'head of cs', 'customer service head'),
    )


def heads_of_sales() -> List:
    return _heads_for(
        'Head of Sales',
        position_keywords=('head of sales', 'sales head'),
    )


# ── Core delivery primitives ───────────────────────────────────────────────

def _send_inapp(recipient, *, title: str, body: str, url: str = '',
                kind: str = 'customer_service', extra: Optional[dict] = None) -> None:
    """Best-effort in-app notification. Silent on any failure."""
    if not recipient:
        return
    Notification = _get_notification_model()
    if not Notification:
        return

    fields = {f.name for f in Notification._meta.get_fields()}

    payload = {}
    # Map our values onto whatever field names this project's model uses.
    for candidate, value in (
        (('user', 'recipient', 'to_user'),                    recipient),
        (('title', 'subject', 'heading'),                     title[:200]),
        (('body', 'message', 'content', 'description'),       body),
        (('url', 'link', 'action_url', 'target_url'),         url),
        (('kind', 'category', 'type', 'notification_type'),   kind),
        (('metadata', 'extra', 'data'),                       extra or {}),
    ):
        for name in candidate:
            if name in fields:
                payload[name] = value
                break

    try:
        Notification.objects.create(**payload)
    except Exception:
        logger.exception('Failed to write in-app notification for %s', recipient)


def _send_email(recipient, *, subject: str, template: str, context: dict) -> None:
    if not recipient:
        return
    to_addr = getattr(recipient, 'email', None)
    if not to_addr:
        return

    try:
        html = render_to_string(template, context)
    except Exception:
        # Fall back to a plain rendering of the context's "body" if available
        html = (context.get('body') or context.get('summary') or subject)

    text = _strip_html(html)

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=text,
            from_email=_from_email(),
            to=[to_addr],
        )
        msg.attach_alternative(html, 'text/html')
        msg.send(fail_silently=True)
    except Exception:
        logger.exception('Failed to send email to %s', to_addr)


def _strip_html(html: str) -> str:
    import re
    txt = re.sub(r'<style.*?</style>', '', html or '', flags=re.S | re.I)
    txt = re.sub(r'<script.*?</script>', '', txt, flags=re.S | re.I)
    txt = re.sub(r'<[^>]+>', '', txt)
    return re.sub(r'\n{3,}', '\n\n', txt).strip()


# ── EO messaging channel (DM inside apps.messaging) ───────────────────────

def _get_or_create_dm(sender, recipient):
    """Find (or create) the 1-on-1 direct ChatRoom between two users."""
    from apps.messaging.models import ChatRoom, ChatRoomMember
    room = (
        ChatRoom.objects
        .filter(room_type=ChatRoom.RoomType.DIRECT, members=sender)
        .filter(members=recipient)
        .first()
    )
    if room:
        return room
    room = ChatRoom.objects.create(
        room_type=ChatRoom.RoomType.DIRECT,
        created_by=sender,
    )
    ChatRoomMember.objects.bulk_create([
        ChatRoomMember(room=room, user=sender),
        ChatRoomMember(room=room, user=recipient),
    ])
    return room


def _send_eo_chat(recipient, *, sender, body: str, url: str = '') -> None:
    """
    Best-effort message into the EO messaging app: a DM from `sender`
    (the user who triggered the event) to `recipient`. Reuses the
    messaging app's own serializer / push fan-out so an open chat updates
    live and offline users get the normal push/email the chat sends.
    """
    if not recipient or not sender or getattr(sender, 'pk', None) == recipient.pk:
        return
    try:
        from apps.messaging.models import ChatMessage
        room = _get_or_create_dm(sender, recipient)
        content = body if not url else f'{body}\n{url}'
        msg = ChatMessage.objects.create(
            room=room,
            sender=sender,
            content=content,
            message_type='text',
        )
        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])

        try:
            from apps.messaging.views import _notify_offline_members
            _notify_offline_members(room, sender, msg)
        except Exception:
            pass

        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            from apps.messaging.views import _serialize_chat_message
            layer = get_channel_layer()
            if layer is not None:
                async_to_sync(layer.group_send)(
                    f'chat_{room.id}',
                    {'type': 'chat.message',
                     'payload': _serialize_chat_message(msg, viewer=recipient)},
                )
        except Exception:
            pass
    except Exception:
        logger.exception('EO chat notification failed for user %s',
                         getattr(recipient, 'id', '?'))


def _notify_users(users: Iterable, *, title: str, body: str, url: str,
                  kind: str, email_subject: str, email_template: str,
                  email_context: dict, eo_sender=None) -> int:
    """
    Fan a notification out to `users` on in-app + email — and, when
    `eo_sender` is given, ALSO as an EO messaging DM from that user.
    """
    seen = set()
    sent = 0
    for u in users:
        if not u or u.pk in seen:
            continue
        seen.add(u.pk)
        _send_inapp(u, title=title, body=body, url=url, kind=kind)
        _send_email(u, subject=email_subject, template=email_template, context=email_context)
        if eo_sender is not None:
            _send_eo_chat(u, sender=eo_sender, body=f'{title}\n{body}', url=url)
        sent += 1
    return sent


# ── CS unit membership ─────────────────────────────────────────────────────

def cs_unit_members() -> List:
    """
    Every active user in the Customer Service unit — matched on the same
    dual group/position-title mechanism as permissions.py.
    """
    from .permissions import CS_TEAM_GROUPS, CS_MANAGER_GROUPS
    groups = CS_TEAM_GROUPS | CS_MANAGER_GROUPS

    matched = set()
    for u in User.objects.filter(is_active=True).prefetch_related('groups'):
        names = {g.name.lower() for g in u.groups.all()}
        if names & groups:
            matched.add(u)
            continue
        try:
            title = (u.staffprofile.position.title or '').lower()
        except Exception:
            title = ''
        if any(kw in title for kw in ('customer service', 'head of cs', 'support')):
            matched.add(u)
    return list(matched)


# ════════════════════════════════════════════════════════════════════════
# PUBLIC EVENTS
# ════════════════════════════════════════════════════════════════════════

# ── New-ticket broadcast + claim + owner-scoped activity ──────────────────

def notify_new_ticket(ticket, *, actor=None) -> int:
    """
    A ticket was just created → EVERY active CS rep is notified on all
    three channels (in-app + email + EO messaging DM) so anyone can claim
    it. The creator (actor) is excluded from the fan-out.
    """
    url = _ticket_url(ticket)
    title = f'New ticket — {ticket.ticket_no}'
    body = (
        f'New {ticket.get_ticket_type_display()} ticket {ticket.ticket_no} '
        f'("{ticket.subject}") for {ticket.customer.display_name} is waiting '
        f'in the queue. Open it and claim it if you can take it.'
    )
    ctx = {'ticket': ticket, 'ticket_url': url,
           'actor_name': _user_full_name(actor), 'body': body}

    recipients = [u for u in cs_unit_members()
                  if not (actor and u.pk == actor.pk)]
    return _notify_users(
        recipients,
        title=title, body=body, url=url,
        kind='cs.ticket.new',
        email_subject=f'[New Ticket] {ticket.ticket_no} — {ticket.subject}',
        email_template='customer_service/email/ticket_new.html',
        email_context=ctx,
        eo_sender=actor,
    )


def notify_ticket_claimed(ticket, *, actor) -> None:
    """
    A rep claimed an unassigned ticket. Tell the ticket creator (so they
    know who picked it up) and the Head of CS for visibility.
    """
    url = _ticket_url(ticket)
    actor_name = _user_full_name(actor)
    title = f'Ticket claimed — {ticket.ticket_no}'
    body = f'{actor_name} claimed ticket {ticket.ticket_no} ("{ticket.subject}").'
    ctx = {'ticket': ticket, 'ticket_url': url, 'actor_name': actor_name, 'body': body}

    recipients = set()
    if ticket.created_by and ticket.created_by.pk != actor.pk:
        recipients.add(ticket.created_by)
    for h in heads_of_customer_service():
        if h.pk != actor.pk:
            recipients.add(h)

    _notify_users(
        recipients,
        title=title, body=body, url=url,
        kind='cs.ticket.claimed',
        email_subject=f'[Ticket {ticket.ticket_no}] Claimed by {actor_name}',
        email_template='customer_service/email/ticket_new.html',
        email_context=ctx,
        eo_sender=actor,
    )


def notify_ticket_activity(ticket, *, actor=None, title: str, body: str,
                           email_subject: str = '') -> int:
    """
    Generic activity ping (note added, customer replied, status change,
    live-chat message…).

    Routing rule:
      * Ticket HAS an owner → notify ONLY the owner and the ticket
        creator (never the actor themselves).
      * Ticket has NO owner yet → notify the whole CS unit (plus the
        creator), since nobody owns it and someone should react.

    All three channels: in-app + email + EO messaging DM.
    """
    url = _ticket_url(ticket)
    ctx = {'ticket': ticket, 'ticket_url': url,
           'actor_name': _user_full_name(actor), 'body': body}

    recipients = set()
    if ticket.current_owner_id:
        recipients.add(ticket.current_owner)
        if ticket.created_by_id:
            recipients.add(ticket.created_by)
    else:
        recipients.update(cs_unit_members())
        if ticket.created_by_id:
            recipients.add(ticket.created_by)

    if actor is not None:
        recipients = {u for u in recipients if u.pk != actor.pk}

    return _notify_users(
        recipients,
        title=title, body=body, url=url,
        kind='cs.ticket.activity',
        email_subject=email_subject or f'[Ticket {ticket.ticket_no}] {title}',
        email_template='customer_service/email/ticket_new.html',
        email_context=ctx,
        eo_sender=actor,
    )


# ── Assignment events ─────────────────────────────────────────────────────

def notify_assignment_created(assignment, *, actor=None) -> None:
    """
    Single-user assignment. Notifies the assignee.
    Also CCs the head of CS so they have visibility into all CS assignments.
    """
    ticket = assignment.ticket
    url = _ticket_url(ticket)
    actor_name = _user_full_name(actor)

    ctx = {
        'assignment': assignment,
        'ticket': ticket,
        'actor_name': actor_name,
        'ticket_url': url,
    }

    if assignment.assigned_to:
        title = f'Ticket assigned to you: {ticket.ticket_no}'
        body = (
            f'{actor_name} assigned you to ticket {ticket.ticket_no} — '
            f'{ticket.subject}.'
        )
        _send_inapp(
            assignment.assigned_to,
            title=title, body=body, url=url, kind='cs.assignment',
            extra={'ticket_id': ticket.pk, 'assignment_id': assignment.pk},
        )
        _send_email(
            assignment.assigned_to,
            subject=f'[Ticket {ticket.ticket_no}] You have a new assignment',
            template='customer_service/email/assignment_created.html',
            context=ctx,
        )
        # EO messaging DM from the person who assigned it
        _send_eo_chat(
            assignment.assigned_to,
            sender=actor,
            body=f'{title}\n{body}',
            url=url,
        )

    # Head of CS visibility
    for head in heads_of_customer_service():
        if assignment.assigned_to and head.pk == assignment.assigned_to.pk:
            continue
        _send_inapp(
            head,
            title=f'CS assignment created — {ticket.ticket_no}',
            body=f'{actor_name} assigned {_user_full_name(assignment.assigned_to)} '
                 f'to ticket {ticket.ticket_no}.',
            url=url,
            kind='cs.assignment.head',
        )


def notify_route_to_department(ticket, *, to_department, from_department=None,
                               actor=None, note: str = '') -> int:
    """
    Routing event. Notifies EVERYONE in the destination department, plus the
    department head and the head of CS for visibility.

    Returns the number of unique users notified.
    """
    if not to_department:
        return 0

    url = _ticket_url(ticket)
    actor_name = _user_full_name(actor)
    members = _users_in_department(to_department)

    title = f'Ticket routed to your department: {ticket.ticket_no}'
    body = (
        f'{actor_name} routed ticket {ticket.ticket_no} '
        f'({ticket.subject}) to {to_department.name}.'
    )
    if note:
        body += f'\n\nNote: {note}'

    ctx = {
        'ticket': ticket,
        'to_department': to_department,
        'from_department': from_department,
        'actor_name': actor_name,
        'note': note,
        'ticket_url': url,
    }

    sent = _notify_users(
        members,
        title=title, body=body, url=url,
        kind='cs.route',
        email_subject=f'[Ticket {ticket.ticket_no}] Routed to {to_department.name}',
        email_template='customer_service/email/ticket_routed.html',
        email_context=ctx,
    )

    # Head of CS — always informed of routing
    member_pks = {m.pk for m in members}
    extra_heads = [h for h in heads_of_customer_service() if h.pk not in member_pks]
    _notify_users(
        extra_heads,
        title=f'CS routing event — {ticket.ticket_no}',
        body=f'{actor_name} routed {ticket.ticket_no} to {to_department.name}.',
        url=url, kind='cs.route.head',
        email_subject=f'[CS] Ticket {ticket.ticket_no} routed to {to_department.name}',
        email_template='customer_service/email/ticket_routed.html',
        email_context=ctx,
    )

    # Sales head sees everything ticket-related too
    sales_heads = [h for h in heads_of_sales() if h.pk not in member_pks]
    for h in sales_heads:
        _send_inapp(
            h,
            title=f'Ticket activity — {ticket.ticket_no} routed',
            body=body, url=url, kind='cs.route.sales_head',
        )

    return sent


def notify_assignment_completed(assignment, *, actor=None) -> None:
    """
    Fired when a ServiceTicketAssignment moves to resolved/closed.
    Notifies:
        • Head of CS (always)
        • Head of the originating department (the assignment.department)
        • Ticket creator + current owner so they can confirm and close
    """
    ticket = assignment.ticket
    url = _ticket_url(ticket)
    actor_name = _user_full_name(actor)

    title = f'Assignment completed — {ticket.ticket_no}'
    body = (
        f'{actor_name} marked the assignment "{assignment.title}" as '
        f'{assignment.get_status_display()}. '
        f'Awaiting confirmation from the ticket owner before close.'
    )

    ctx = {
        'assignment': assignment,
        'ticket': ticket,
        'actor_name': actor_name,
        'ticket_url': url,
    }

    recipients = set()

    # Department head (StaffProfile-based)
    if assignment.department:
        recipients.update(_department_heads(assignment.department))

    # Heads of CS — always
    recipients.update(heads_of_customer_service())

    # Ticket creator + current owner (so they can confirm)
    if ticket.created_by:
        recipients.add(ticket.created_by)
    if ticket.current_owner and ticket.current_owner != ticket.created_by:
        recipients.add(ticket.current_owner)

    _notify_users(
        recipients,
        title=title, body=body, url=url,
        kind='cs.assignment.completed',
        email_subject=f'[Ticket {ticket.ticket_no}] Assignment completed — needs confirmation',
        email_template='customer_service/email/assignment_completed.html',
        email_context=ctx,
    )


def _department_heads(department) -> List:
    """Department head — best-effort lookup via Django group naming or position."""
    if not department:
        return []
    name = department.name
    return _heads_for(
        f'Head of {name}', f'{name} Head', f'{name} Manager',
        position_keywords=(f'head of {name.lower()}', f'{name.lower()} head',
                           f'{name.lower()} manager'),
    )


# ── SLA / overdue events ──────────────────────────────────────────────────

def notify_ticket_overdue(ticket) -> None:
    """
    Heads-up to the head of Sales (and head of CS) that a ticket has
    breached its resolution SLA. Triggered by the management command.
    """
    url = _ticket_url(ticket)
    title = f'Overdue ticket — {ticket.ticket_no}'
    body = (
        f'Ticket {ticket.ticket_no} ({ticket.subject}) is past its '
        f'resolution due date ({ticket.resolution_due_at:%Y-%m-%d %H:%M}). '
        f'Status: {ticket.get_status_display()}.'
    )

    ctx = {'ticket': ticket, 'ticket_url': url}

    recipients = set(heads_of_sales()) | set(heads_of_customer_service())
    if ticket.current_owner:
        recipients.add(ticket.current_owner)

    _notify_users(
        recipients,
        title=title, body=body, url=url,
        kind='cs.ticket.overdue',
        email_subject=f'[Overdue] Ticket {ticket.ticket_no}',
        email_template='customer_service/email/ticket_overdue.html',
        email_context=ctx,
    )


# ── Order-from-call events ────────────────────────────────────────────────

def notify_order_created_from_ticket(ticket, order, *, actor=None) -> None:
    """
    Fired by services.create_order_from_ticket() once an order is born from
    a CS ticket / call. Lets the head of CS and head of Sales see the link.
    """
    url = _ticket_url(ticket)
    actor_name = _user_full_name(actor)

    try:
        order_url = _absolute_url(reverse('orders:order_detail', kwargs={'pk': order.pk}))
    except NoReverseMatch:
        # Don't leave the email button with an empty href — fall back to
        # the ticket detail page, which always resolves.
        order_url = url

    title = f'Order {order.order_no} created from ticket {ticket.ticket_no}'
    body = (
        f'{actor_name} created sales order {order.order_no} '
        f'(total: {order.currency} {order.total}) from ticket {ticket.ticket_no}.'
    )

    recipients = set(heads_of_customer_service()) | set(heads_of_sales())
    if ticket.created_by:
        recipients.add(ticket.created_by)

    for u in recipients:
        _send_inapp(
            u, title=title, body=body, url=order_url or url,
            kind='cs.order.created',
            extra={'ticket_id': ticket.pk, 'order_id': str(order.pk)},
        )

    # Email only the heads — agents don't need an email about their own action
    head_set = set(heads_of_customer_service()) | set(heads_of_sales())
    ctx = {'ticket': ticket, 'order': order, 'actor_name': actor_name,
           'ticket_url': url, 'order_url': order_url}
    for u in head_set:
        _send_email(
            u,
            subject=f'[Order {order.order_no}] Created from ticket {ticket.ticket_no}',
            template='customer_service/email/order_from_ticket.html',
            context=ctx,
        )


# ── Convenience: notify ticket creator/owner on confirmation request ──────

def notify_ticket_ready_to_close(ticket, *, assignment=None, actor=None) -> None:
    """
    All assignments have completed. Ping the ticket creator + current owner
    to ask them to confirm + close.
    """
    url = _ticket_url(ticket)
    title = f'Ticket {ticket.ticket_no} — ready to close'
    body = (
        f'All assignments on ticket {ticket.ticket_no} are now complete. '
        f'Please confirm with the customer and close the ticket.'
    )
    ctx = {'ticket': ticket, 'assignment': assignment, 'ticket_url': url,
           'actor_name': _user_full_name(actor)}

    targets = []
    if ticket.created_by:
        targets.append(ticket.created_by)
    if ticket.current_owner and ticket.current_owner != ticket.created_by:
        targets.append(ticket.current_owner)

    _notify_users(
        targets,
        title=title, body=body, url=url, kind='cs.ticket.ready_to_close',
        email_subject=f'[Ticket {ticket.ticket_no}] Ready to close',
        email_template='customer_service/email/ticket_ready_to_close.html',
        email_context=ctx,
    )