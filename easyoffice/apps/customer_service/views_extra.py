"""
apps/customer_service/views_extra.py
────────────────────────────────────
New views to add on top of your existing customer_service.views.

They cover five flows:

    1. CreateOrderFromTicketView       — turns a ticket / call into a SalesOrder
    2. CompleteAssignmentView          — department-side "we've finished this"
    3. ConfirmAndCloseTicketView       — owner-side "customer confirmed, close it"
    4. HeadDashboardView + reports     — Head of CS / Head of Sales oversight
    5. PortalReplyView                 — staff reply into customer portal thread

WIRE UP
───────
1. In urls_extra.py (already done) add:
       path('tickets/<int:pk>/portal-reply/',
            views_extra.PortalReplyView.as_view(),
            name='customer_service_portal_reply'),

2. In your existing TicketDetailView.get_context_data(), add:
       from .views_extra import get_portal_context
       ctx.update(get_portal_context(self.object))

   This injects four template variables:
       portal_request        — linked PortalSupportRequest (or None)
       portal_comments       — ordered QS of TicketComment for that request
       portal_comment_count  — total message count (int)
       portal_unread_count   — customer messages since last staff reply (int)

3. In your existing TicketActionView.post(), after every status change call:
       from .views_extra import mirror_cs_status_to_portal
       mirror_cs_status_to_portal(ticket, ticket.status)

   This keeps the portal request status in sync when you resolve/close/cancel
   on the CS side, and leaves a system comment the customer can see.
"""
from __future__ import annotations

import csv
import logging
from decimal import Decimal
from io import StringIO

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q, Avg
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.generic import View, TemplateView

from .models import (
    ServiceTicket, ServiceTicketAssignment,
    CallRecord, Customer, FeedbackRequest, ServiceRating,
)
from .permissions import (
    CustomerServiceAccessMixin, CustomerServiceOversightMixin,
    can_close_ticket, is_head_of_customer_service, is_head_of_sales,
)
from . import services

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════
# HELPER: get_portal_context
# Call from your existing TicketDetailView.get_context_data()
# ════════════════════════════════════════════════════════════════════════

def get_portal_context(ticket) -> dict:
    """
    Returns a dict ready to merge into your template context:

        portal_request        — PortalSupportRequest linked to this ServiceTicket (or None)
        portal_comments       — ordered QS of TicketComment objects
        portal_comment_count  — integer total
        portal_unread_count   — customer messages that arrived after the most recent
                                staff reply (signals "new unread" to the agent)

    Usage in TicketDetailView:
        from .views_extra import get_portal_context
        ctx.update(get_portal_context(self.object))
    """
    empty = {
        'portal_request': None,
        'portal_comments': [],
        'portal_comment_count': 0,
        'portal_unread_count': 0,
    }
    try:
        from apps.customer_portal.models import (
            PortalSupportRequest,
            TicketComment as PortalComment,
        )
        portal_req = (
            PortalSupportRequest.objects
            .filter(service_ticket=ticket)
            .select_related('contact')
            .first()
        )
        if not portal_req:
            return empty

        comments = (
            PortalComment.objects
            .filter(request=portal_req)
            .select_related('author_contact', 'author_user')
            .prefetch_related('attachments')
            .order_by('created_at')
        )

        # "Unread" = customer messages after the last staff reply.
        last_staff = (
            PortalComment.objects
            .filter(request=portal_req, author_kind='staff')
            .order_by('-created_at')
            .first()
        )
        unread_qs = PortalComment.objects.filter(
            request=portal_req, author_kind='customer'
        )
        if last_staff:
            unread_qs = unread_qs.filter(created_at__gt=last_staff.created_at)
        unread_count = unread_qs.count()

        return {
            'portal_request': portal_req,
            'portal_comments': comments,
            'portal_comment_count': comments.count(),
            'portal_unread_count': unread_count,
        }
    except Exception:
        logger.exception('customer_service: get_portal_context failed')
        return empty


# ════════════════════════════════════════════════════════════════════════
# HELPER: mirror_cs_status_to_portal
# Call from TicketActionView after every status-changing action
# ════════════════════════════════════════════════════════════════════════

_CS_TO_PORTAL_STATUS = {
    'resolved':  'resolved',
    'closed':    'closed',
    'cancelled': 'cancelled',
}

_PORTAL_STATUS_MESSAGE = {
    'resolved': (
        'Your support request has been marked resolved by our team. '
        'Please use the Rate button on this page to leave feedback — it helps us improve.'
    ),
    'closed': (
        'Your support request has been closed by our team. '
        'If you have a new issue, please submit a fresh request from your portal.'
    ),
    'cancelled': (
        'Your support request has been cancelled. '
        'If this was unexpected, please contact us or submit a new request.'
    ),
}


def mirror_cs_status_to_portal(ticket, new_cs_status: str) -> None:
    """
    Mirrors a CS ticket status change to the linked PortalSupportRequest
    and leaves a system comment the customer can see.

    Call this from your existing TicketActionView.post() after saving
    the new ticket status:

        from .views_extra import mirror_cs_status_to_portal
        mirror_cs_status_to_portal(ticket, ticket.status)
    """
    portal_status = _CS_TO_PORTAL_STATUS.get(new_cs_status)
    if not portal_status:
        return  # status change doesn't need mirroring (e.g. assigned, in_progress)

    try:
        from apps.customer_portal.models import PortalSupportRequest, TicketComment as PortalComment

        portal_req = (
            PortalSupportRequest.objects
            .filter(service_ticket=ticket)
            .first()
        )
        if not portal_req:
            return
        if portal_req.status in ('closed', 'cancelled'):
            return  # already terminal

        now = timezone.now()
        update_fields = ['status', 'updated_at']

        portal_req.status = portal_status
        if portal_status == 'resolved' and not getattr(portal_req, 'resolved_at', None):
            portal_req.resolved_at = now
            update_fields.append('resolved_at')
        elif portal_status == 'closed' and not getattr(portal_req, 'closed_at', None):
            portal_req.closed_at = now
            update_fields.append('closed_at')
        elif portal_status == 'cancelled' and not getattr(portal_req, 'cancelled_at', None):
            portal_req.cancelled_at = now
            update_fields.append('cancelled_at')

        portal_req.save(update_fields=update_fields)

        PortalComment.objects.create(
            request=portal_req,
            author_kind='system',
            author_display_name='System',
            event_tag=portal_status,
            body=_PORTAL_STATUS_MESSAGE[portal_status],
        )
    except Exception:
        logger.exception('customer_service: mirror_cs_status_to_portal failed')


# ════════════════════════════════════════════════════════════════════════
# 1. CREATE ORDER FROM TICKET
# ════════════════════════════════════════════════════════════════════════

class CreateOrderFromTicketView(CustomerServiceAccessMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        ticket = get_object_or_404(ServiceTicket, pk=pk)

        items = self._collect_items(request.POST)
        if not items:
            messages.error(request, 'Add at least one line item before creating an order.')
            return redirect('customer_service_ticket_detail', pk=ticket.pk)

        try:
            from apps.orders import services as orders_services
            order = orders_services.create_order_from_ticket(
                ticket,
                actor=request.user,
                items=items,
                delivery_address=request.POST.get('delivery_address', '').strip(),
                notes=request.POST.get('notes', '').strip(),
                currency=(request.POST.get('currency') or 'GMD').strip()[:3],
                tax_rate=Decimal(request.POST.get('tax_rate') or '0'),
                discount_amount=Decimal(request.POST.get('discount_amount') or '0'),
                contact_overrides={
                    'contact_name':  request.POST.get('contact_name', '').strip(),
                    'contact_phone': request.POST.get('contact_phone', '').strip(),
                    'contact_email': request.POST.get('contact_email', '').strip(),
                },
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('customer_service_ticket_detail', pk=ticket.pk)
        except Exception as exc:
            logger.exception('create_order_from_ticket failed for ticket %s', ticket.pk)
            messages.error(request, f'Could not create order: {exc}')
            return redirect('customer_service_ticket_detail', pk=ticket.pk)

        messages.success(request, f'Order {order.order_no} created from this ticket.')
        try:
            from django.urls import reverse
            return redirect(reverse('orders:order_detail', kwargs={'pk': order.pk}))
        except Exception:
            return redirect('customer_service_ticket_detail', pk=ticket.pk)

    @staticmethod
    def _collect_items(post) -> list:
        items: list = []

        descs  = post.getlist('item_description') or post.getlist('item_description[]')
        qtys   = post.getlist('item_quantity')    or post.getlist('item_quantity[]')
        prices = post.getlist('item_unit_price')  or post.getlist('item_unit_price[]')
        prods  = post.getlist('item_product_id')  or post.getlist('item_product_id[]')

        for i, desc in enumerate(descs):
            d = (desc or '').strip()
            try:
                q = Decimal(qtys[i]) if i < len(qtys) and qtys[i] else Decimal('0')
            except Exception:
                q = Decimal('0')
            try:
                p = Decimal(prices[i]) if i < len(prices) and prices[i] else Decimal('0')
            except Exception:
                p = Decimal('0')
            if not d and q == 0:
                continue
            line = {'description': d, 'quantity': q, 'unit_price': p}
            if i < len(prods) and prods[i]:
                line['product_id'] = prods[i]
            items.append(line)

        if items:
            return items

        i = 0
        while True:
            desc = post.get(f'items-{i}-description')
            if desc is None:
                break
            d = desc.strip()
            try:
                q = Decimal(post.get(f'items-{i}-quantity', '0') or '0')
            except Exception:
                q = Decimal('0')
            try:
                p = Decimal(post.get(f'items-{i}-unit_price', '0') or '0')
            except Exception:
                p = Decimal('0')
            if not (not d and q == 0):
                line = {'description': d, 'quantity': q, 'unit_price': p}
                pid = post.get(f'items-{i}-product_id')
                if pid:
                    line['product_id'] = pid
                items.append(line)
            i += 1

        return items


# ════════════════════════════════════════════════════════════════════════
# 2. COMPLETE ASSIGNMENT
# ════════════════════════════════════════════════════════════════════════

class CompleteAssignmentView(CustomerServiceAccessMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        assignment = get_object_or_404(ServiceTicketAssignment, pk=pk)

        user = request.user
        allowed = (
            user.is_superuser
            or assignment.assigned_to_id == user.pk
            or self._user_in_dept(user, assignment.department)
            or is_head_of_customer_service(user)
        )
        if not allowed:
            messages.error(request, 'You are not allowed to complete this assignment.')
            return redirect('customer_service_ticket_detail', pk=assignment.ticket_id)

        note = (request.POST.get('resolution_note') or '').strip()
        try:
            services.complete_assignment(
                assignment, actor=user, resolution_note=note, final_status='resolved',
            )
        except Exception as exc:
            messages.error(request, f'Could not mark complete: {exc}')
            return redirect('customer_service_ticket_detail', pk=assignment.ticket_id)

        messages.success(request, 'Assignment marked complete. The ticket owner has been notified.')
        return redirect('customer_service_ticket_detail', pk=assignment.ticket_id)

    @staticmethod
    def _user_in_dept(user, department) -> bool:
        if not department:
            return False
        try:
            return user.staffprofile.department_id == department.pk
        except Exception:
            return False


# ════════════════════════════════════════════════════════════════════════
# 3. CONFIRM & CLOSE TICKET
# ════════════════════════════════════════════════════════════════════════

class ConfirmAndCloseTicketView(CustomerServiceAccessMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        ticket = get_object_or_404(ServiceTicket, pk=pk)
        if not can_close_ticket(request.user, ticket):
            messages.error(request, 'Only the ticket owner or a supervisor can close this ticket.')
            return redirect('customer_service_ticket_detail', pk=ticket.pk)

        note = (request.POST.get('confirmation_note') or '').strip()
        force = bool(request.POST.get('force')) and (
            request.user.is_superuser or is_head_of_customer_service(request.user)
        )

        try:
            services.confirm_and_close_ticket(
                ticket, actor=request.user, confirmation_note=note, force=force,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('customer_service_ticket_detail', pk=ticket.pk)

        # Mirror the closed status to the portal.
        mirror_cs_status_to_portal(ticket, 'closed')

        messages.success(request, f'Ticket {ticket.ticket_no} closed.')
        return redirect('customer_service_ticket_detail', pk=ticket.pk)


# ════════════════════════════════════════════════════════════════════════
# 4. HEAD DASHBOARD + REPORTS
# ════════════════════════════════════════════════════════════════════════

class HeadDashboardView(CustomerServiceOversightMixin, TemplateView):
    template_name = 'customer_service/oversight.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        now = timezone.now()
        last_30 = now - timezone.timedelta(days=30)

        ctx['is_cs_head']    = is_head_of_customer_service(user) or user.is_superuser
        ctx['is_sales_head'] = is_head_of_sales(user)            or user.is_superuser

        tickets_qs = ServiceTicket.objects.all()

        ctx['totals'] = {
            'open': tickets_qs.exclude(status__in=('resolved', 'closed', 'cancelled')).count(),
            'overdue': tickets_qs.filter(
                resolution_due_at__lt=now,
            ).exclude(status__in=('resolved', 'closed', 'cancelled')).count(),
            'resolved_30d': tickets_qs.filter(resolved_at__gte=last_30).count(),
            'cancelled_30d': tickets_qs.filter(status='cancelled', updated_at__gte=last_30).count(),
            'calls_30d': CallRecord.objects.filter(started_at__gte=last_30).count(),
            'customers': Customer.objects.count(),
        }

        ctx['status_breakdown'] = list(
            tickets_qs.values('status').annotate(n=Count('id')).order_by('-n')
        )
        ctx['by_department'] = list(
            tickets_qs.exclude(status__in=('closed', 'cancelled'))
            .values('department__name')
            .annotate(n=Count('id'), overdue=Count('id', filter=Q(resolution_due_at__lt=now)))
            .order_by('-n')[:15]
        )
        ctx['overdue_tickets'] = list(
            tickets_qs.filter(resolution_due_at__lt=now)
            .exclude(status__in=('resolved', 'closed', 'cancelled'))
            .select_related('customer', 'department', 'current_owner')
            .order_by('resolution_due_at')[:25]
        )
        ctx['recent_calls'] = list(
            CallRecord.objects.select_related('customer', 'handled_by').order_by('-started_at')[:15]
        )
        ctx['recent_tickets'] = list(
            tickets_qs.select_related('customer', 'department').order_by('-created_at')[:15]
        )
        ctx['stale_assignments'] = list(
            ServiceTicketAssignment.objects
            .filter(sla_due_at__lt=now)
            .exclude(status__in=('resolved', 'closed'))
            .select_related('ticket', 'department', 'assigned_to')
            .order_by('sla_due_at')[:20]
        )

        if ctx['is_cs_head']:
            ratings_qs = ServiceRating.objects.filter(created_at__gte=last_30)
            ctx['ratings_summary'] = ratings_qs.aggregate(
                avg_service=Avg('service_quality'),
                avg_product=Avg('product_quality'),
                avg_response=Avg('response_time'),
                avg_professionalism=Avg('professionalism'),
                count=Count('id'),
            )
            ctx['recent_ratings'] = list(
                ratings_qs.select_related('ticket', 'customer').order_by('-created_at')[:10]
            )

        return ctx


class HeadReportCSVView(CustomerServiceOversightMixin, View):
    def get(self, request):
        date_from, date_to = self._range(request)

        tickets = (
            ServiceTicket.objects
            .filter(created_at__gte=date_from, created_at__lt=date_to)
            .select_related('customer', 'department', 'current_owner', 'created_by')
            .order_by('created_at')
        )
        is_cs_head = is_head_of_customer_service(request.user) or request.user.is_superuser

        buf = StringIO()
        w = csv.writer(buf)
        header = [
            'Ticket No', 'Subject', 'Type', 'Priority', 'Status',
            'Customer', 'Department', 'Created By', 'Owner',
            'Created At', 'Resolution Due', 'Resolved At', 'Closed At', 'Overdue?',
        ]
        if is_cs_head:
            header += ['Call Linked', 'Callback Required', 'Feedback Sent', 'Avg Rating']
        w.writerow(header)

        for t in tickets:
            row = [
                t.ticket_no, t.subject,
                t.get_ticket_type_display(), t.get_priority_display(), t.get_status_display(),
                t.customer.display_name if t.customer_id else '',
                t.department.name if t.department_id else '',
                t.created_by.get_full_name() if t.created_by_id else '',
                t.current_owner.get_full_name() if t.current_owner_id else '',
                t.created_at.strftime('%Y-%m-%d %H:%M') if t.created_at else '',
                t.resolution_due_at.strftime('%Y-%m-%d %H:%M') if t.resolution_due_at else '',
                t.resolved_at.strftime('%Y-%m-%d %H:%M') if t.resolved_at else '',
                t.closed_at.strftime('%Y-%m-%d %H:%M') if t.closed_at else '',
                'Yes' if t.is_overdue else 'No',
            ]
            if is_cs_head:
                rating = getattr(t, 'rating', None)
                avg_rating = ''
                if rating:
                    parts = [rating.service_quality or 0, rating.product_quality or 0,
                             rating.response_time or 0, rating.professionalism or 0]
                    nonzero = [p for p in parts if p]
                    if nonzero:
                        avg_rating = round(sum(nonzero) / len(nonzero), 2)
                feedback = FeedbackRequest.objects.filter(ticket=t).first()
                row += [
                    'Yes' if t.call_id else 'No',
                    'Yes' if t.callback_required else 'No',
                    'Yes' if feedback and feedback.sent_at else 'No',
                    avg_rating,
                ]
            w.writerow(row)

        resp = HttpResponse(buf.getvalue(), content_type='text/csv')
        resp['Content-Disposition'] = (
            f'attachment; filename="cs-report-{date_from:%Y%m%d}-{date_to:%Y%m%d}.csv"'
        )
        return resp

    @staticmethod
    def _range(request):
        from datetime import datetime
        now = timezone.now()
        try:
            df = datetime.fromisoformat(request.GET['from']).replace(tzinfo=now.tzinfo)
        except Exception:
            df = now - timezone.timedelta(days=30)
        try:
            dt = datetime.fromisoformat(request.GET['to']).replace(tzinfo=now.tzinfo)
        except Exception:
            dt = now
        return df, dt


# ════════════════════════════════════════════════════════════════════════
# 5. PORTAL REPLY — staff reply into customer portal thread
# ════════════════════════════════════════════════════════════════════════

class PortalReplyView(CustomerServiceAccessMixin, View):
    """
    POST /customer-service/tickets/<pk>/portal-reply/

    Saves a staff reply visible to the customer in their portal.
    Also promotes pending_review → open and emails the customer.
    """
    http_method_names = ['post']

    # Reply-body field names accepted from POST. Two templates submit here:
    #   * _portal_thread.html       → posts 'portal_body' (bottom composer
    #                                  + inline-on-customer-bubble form)
    #   * _portal_reply_button.html → posts 'body'        (per-update inline
    #                                  reply button on the activity timeline)
    # Either should just work — never assume one specific name.
    _BODY_FIELDS = ('portal_body', 'body', 'reply_body')

    def post(self, request, pk):
        ticket = get_object_or_404(ServiceTicket, pk=pk)

        body = ''
        for name in self._BODY_FIELDS:
            val = (request.POST.get(name) or '').strip()
            if val:
                body = val
                break

        # Optional in-page anchor so the agent lands back at the comment
        # they were replying to (set by the inline reply button).
        return_to = (request.POST.get('return_to') or '').strip()
        if not return_to.startswith('#'):
            return_to = ''

        if not body:
            messages.error(request, 'Reply body cannot be empty.')
            return self._redirect_back(ticket, return_to)

        portal_req = self._get_portal_request(ticket)
        if not portal_req:
            messages.error(request, 'No linked customer portal request found for this ticket.')
            return self._redirect_back(ticket, return_to)

        if portal_req.status in ('closed', 'cancelled'):
            messages.warning(
                request,
                f'The portal request is {portal_req.get_status_display().lower()} — reply not sent.',
            )
            return self._redirect_back(ticket, return_to)

        with transaction.atomic():
            # Try to grab the portal sync helpers — they give us:
            #   * mirror_staff_reply_to_portal: writes the customer-visible
            #     TicketComment with the re-entrancy guard set
            #   * _SyncContext: suppresses the staff→portal auto-mirror
            #     signal that would otherwise duplicate our reply
            try:
                from apps.customer_portal import sync as portal_sync
                _SyncContext = portal_sync._SyncContext
            except Exception:
                portal_sync = None
                _SyncContext = None

            from .models import ServiceTicketUpdate

            # 1. Write the reply on the STAFF side so it appears in this
            #    ticket's activity timeline. Wrap in _SyncContext so the
            #    customer_portal post_save signal sees _is_syncing() and
            #    skips mirroring this to the portal a SECOND time —
            #    we'll do that ourselves explicitly below.
            if _SyncContext is not None:
                with _SyncContext():
                    staff_update = ServiceTicketUpdate.objects.create(
                        ticket=ticket,
                        user=request.user,
                        update_type='customer_update',
                        body=f'📤 Replied to customer via portal:\n\n{body}',
                    )
            else:
                staff_update = ServiceTicketUpdate.objects.create(
                    ticket=ticket,
                    user=request.user,
                    update_type='customer_update',
                    body=f'📤 Replied to customer via portal:\n\n{body}',
                )

            # 2. Mirror to the customer portal so the customer sees the
            #    reply too. The helper itself uses _SyncContext.
            comment = None
            if portal_sync is not None and hasattr(portal_sync, 'mirror_staff_reply_to_portal'):
                comment = portal_sync.mirror_staff_reply_to_portal(
                    portal_request=portal_req,
                    staff_user=request.user,
                    body=body,
                )
            else:
                # Fallback for environments without sync.py helpers.
                from apps.customer_portal.models import TicketComment as PortalComment
                comment = PortalComment.objects.create(
                    request=portal_req,
                    author_kind='staff',
                    author_user=request.user,
                    author_display_name=(
                        request.user.get_full_name() or request.user.username
                    ),
                    body=body,
                )

            if portal_req.status == 'pending_review':
                portal_req.status = 'open'
                portal_req.save(update_fields=['status', 'updated_at'])

        transaction.on_commit(
            lambda: self._notify_customer(portal_req, comment, request.user)
        )

        messages.success(request, 'Reply sent to the customer portal.')
        return self._redirect_back(ticket, return_to)

    @staticmethod
    def _redirect_back(ticket, fragment: str = ''):
        """Redirect to ticket detail, optionally with a #anchor."""
        url = reverse('customer_service_ticket_detail', kwargs={'pk': ticket.pk})
        if fragment:
            url = f'{url}{fragment}'
        return HttpResponseRedirect(url)

    @staticmethod
    def _get_portal_request(ticket):
        try:
            from apps.customer_portal.models import PortalSupportRequest
            return (
                PortalSupportRequest.objects
                .filter(service_ticket=ticket)
                .select_related('contact', 'token_used')
                .first()
            )
        except Exception:
            return None

    @staticmethod
    def _notify_customer(portal_req, comment, actor):
        try:
            contact = portal_req.contact
            if not contact or not contact.email:
                return

            from django.core.mail import send_mail
            from django.conf import settings

            site_url = getattr(settings, 'SITE_URL', '').rstrip('/')
            ticket_url = ''
            try:
                from django.urls import reverse
                token_obj = portal_req.token_used
                if token_obj:
                    path = reverse(
                        'customer_portal_ticket_detail',
                        kwargs={'token': token_obj.token, 'request_id': portal_req.pk},
                    )
                    ticket_url = f'{site_url}{path}'
            except Exception:
                pass

            actor_name = actor.get_full_name() or actor.username
            preview = comment.body[:400] + ('…' if len(comment.body) > 400 else '')

            send_mail(
                subject=f'New reply on your support request: {portal_req.subject}',
                message=(
                    f'Hi {contact.full_name},\n\n'
                    f'{actor_name} from our support team has replied to your request '
                    f'"{portal_req.subject}":\n\n'
                    f'"{preview}"\n\n'
                    + (f'View the full conversation:\n{ticket_url}\n\n' if ticket_url else '')
                    + '— EasyOffice Support'
                ),
                from_email=(
                    getattr(settings, 'DEFAULT_FROM_EMAIL', None) or 'no-reply@example.com'
                ),
                recipient_list=[contact.email],
                fail_silently=True,
            )
        except Exception:
            logger.exception('customer_service: portal reply customer notify failed')

# ════════════════════════════════════════════════════════════════════════
# 6. STANDALONE CUSTOMER (CONTACT) CREATE & EDIT
#    Anyone with `can_create_customer` permission can create / edit a
#    Customer record without first logging an inbound call. Used by
#    CS, CS Head, Sales Head, CEO, admins, superusers.
# ════════════════════════════════════════════════════════════════════════

from django.views.generic import FormView, UpdateView, CreateView


class _CustomerFormBaseView(View):
    """
    Shared rendering / save logic for the standalone Customer form.

    Subclasses set:
        is_edit          — bool; just a context flag for the template
    and provide a way to fetch / build the Customer instance:
        _get_instance(request, **kwargs) -> Customer | None
    """
    template_name = 'customer_service/customer_form.html'
    http_method_names = ['get', 'post']
    is_edit = False

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        from .permissions import can_create_customer
        if not can_create_customer(request.user):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied(
                "You don't have permission to create or edit customer contacts."
            )
        return super().dispatch(request, *args, **kwargs)

    # ── Subclass hooks ─────────────────────────────────────────────
    def _get_instance(self, request, **kwargs):
        return None

    def _page_title(self, instance):
        if instance and instance.pk:
            return f'Edit Contact — {instance.display_name}'
        return 'New Customer Contact'

    # ── Rendering ──────────────────────────────────────────────────
    def _render(self, request, form, formset, instance, *, status=200):
        from django.shortcuts import render
        return render(request, self.template_name, {
            'form': form,
            'phone_formset': formset,
            'is_edit': bool(instance and instance.pk),
            'page_title': self._page_title(instance),
            'customer': instance,
        }, status=status)

    # ── HTTP handlers ──────────────────────────────────────────────
    def get(self, request, *args, **kwargs):
        from .forms import CustomerForm, CustomerPhoneFormSet
        instance = self._get_instance(request, **kwargs)
        if instance and instance.pk:
            form = CustomerForm(instance=instance)
            formset = CustomerPhoneFormSet(instance=instance)
        else:
            form = CustomerForm(initial={
                'customer_type': 'individual',
                'preferred_contact_method': 'phone',
                'needs_feedback': True,
            })
            formset = CustomerPhoneFormSet()
        return self._render(request, form, formset, instance)

    def post(self, request, *args, **kwargs):
        from .forms import CustomerForm, CustomerPhoneFormSet
        instance = self._get_instance(request, **kwargs)

        if instance and instance.pk:
            form = CustomerForm(request.POST, instance=instance)
            formset = CustomerPhoneFormSet(request.POST, instance=instance)
        else:
            form = CustomerForm(request.POST)
            formset = CustomerPhoneFormSet(request.POST)

        if not (form.is_valid() and formset.is_valid()):
            messages.error(request, 'Please fix the errors below and try again.')
            return self._render(request, form, formset, instance, status=400)

        try:
            with transaction.atomic():
                customer = form.save(commit=False)
                # Only set created_by on first save — never overwrite on edit.
                if not customer.pk:
                    customer.created_by = request.user
                customer.save()

                formset.instance = customer
                phones = formset.save(commit=False)
                for f in formset.deleted_objects:
                    f.delete()

                # If no phone is flagged primary but at least one phone was
                # provided, mark the first non-deleted one primary.
                non_deleted = [p for p in phones if p.phone_number]
                if non_deleted and not any(p.is_primary for p in non_deleted):
                    # Don't override existing-saved primaries; only set when
                    # the customer truly has no primary on file.
                    has_existing_primary = (
                        customer.pk
                        and customer.phones.filter(is_primary=True).exists()
                    )
                    if not has_existing_primary:
                        non_deleted[0].is_primary = True

                for p in phones:
                    if not p.phone_number:
                        continue
                    p.customer = customer
                    p.save()

        except Exception as exc:
            logger.exception('Customer form save failed (instance=%s)', getattr(instance, 'pk', None))
            messages.error(request, f'Could not save the contact: {exc}')
            return self._render(request, form, formset, instance, status=500)

        if self.is_edit:
            messages.success(
                request,
                f'Contact "{customer.display_name}" updated.',
            )
        else:
            messages.success(
                request,
                f'Contact "{customer.display_name}" created '
                f'({customer.customer_code}).',
            )
        return redirect('customer_service_customer_detail', pk=customer.pk)


class CustomerCreateView(_CustomerFormBaseView):
    """
    GET  /customer-service/customers/new/
        → renders an empty Customer form + 1-row phone formset.
    POST same URL
        → validates both, persists, redirects to the customer detail page.
    """
    is_edit = False


class CustomerEditView(_CustomerFormBaseView):
    """
    GET  /customer-service/customers/<pk>/edit/
        → renders the Customer form pre-populated with current values
          and the existing phone records as inline rows.
    POST same URL
        → updates the contact, applies phone add/edit/delete, redirects
          to the customer detail page.
    """
    is_edit = True

    def _get_instance(self, request, *, pk, **kwargs):
        return get_object_or_404(Customer, pk=pk)


# ════════════════════════════════════════════════════════════════════════
# 7. CREATE ORDER DIRECTLY FROM A CUSTOMER (no ticket / no call required)
#    GET → simple form on the customer detail page (or a dedicated page)
#    POST → builds the order via apps.orders.services.create_order_from_customer
# ════════════════════════════════════════════════════════════════════════

class CreateOrderFromCustomerView(View):
    """
    GET  /customer-service/customers/<pk>/create-order/
        → render a small order-creation form pre-filled from the customer.

    POST same URL
        → build the SalesOrder via orders.services and redirect to the
          order detail page on success, or back to the form on failure.

    Anyone with customer-service access can use this — same gate as the
    rest of the CS module.
    """
    template_name = 'customer_service/customer_order_create.html'
    http_method_names = ['get', 'post']

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        from .permissions import can_use_customer_service
        if not can_use_customer_service(request.user):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied("You don't have access to Customer Service.")
        return super().dispatch(request, *args, **kwargs)

    def _get_customer(self, pk):
        return get_object_or_404(
            Customer.objects.prefetch_related('phones'),
            pk=pk,
        )

    def get(self, request, pk):
        customer = self._get_customer(pk)
        from django.shortcuts import render
        return render(request, self.template_name, {
            'customer': customer,
            'sources': self._source_choices(),
            'default_currency': 'GMD',
            'page_title': f'New Order — {customer.display_name}',
        })

    def post(self, request, pk):
        customer = self._get_customer(pk)

        items = self._collect_items(request.POST)
        if not items:
            messages.error(
                request,
                'Add at least one line item before creating an order.',
            )
            return redirect(
                'customer_service_create_order_from_customer',
                pk=customer.pk,
            )

        try:
            from apps.orders import services as orders_services
            from apps.orders.models import OrderSource
        except Exception:
            messages.error(request, 'The orders app is unavailable.')
            logger.exception('CreateOrderFromCustomerView: orders import failed')
            return redirect('customer_service_customer_detail', pk=customer.pk)

        try:
            tax_rate = Decimal(request.POST.get('tax_rate') or '0')
        except Exception:
            tax_rate = Decimal('0')
        try:
            discount = Decimal(request.POST.get('discount_amount') or '0')
        except Exception:
            discount = Decimal('0')

        source = (request.POST.get('source') or 'walk_in').strip()
        if source not in {s for s, _ in OrderSource.choices}:
            source = OrderSource.WALK_IN

        try:
            order = orders_services.create_order_from_customer(
                customer,
                actor=request.user,
                items=items,
                delivery_address=request.POST.get('delivery_address', '').strip(),
                notes=request.POST.get('notes', '').strip(),
                currency=(request.POST.get('currency') or 'GMD').strip()[:3],
                tax_rate=tax_rate,
                discount_amount=discount,
                contact_overrides={
                    'contact_name':  request.POST.get('contact_name', '').strip(),
                    'contact_phone': request.POST.get('contact_phone', '').strip(),
                    'contact_email': request.POST.get('contact_email', '').strip(),
                    'delivery_address': request.POST.get('delivery_address', '').strip(),
                },
                source=source,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect(
                'customer_service_create_order_from_customer',
                pk=customer.pk,
            )
        except Exception as exc:
            logger.exception('create_order_from_customer failed (customer=%s)', customer.pk)
            messages.error(request, f'Could not create order: {exc}')
            return redirect(
                'customer_service_create_order_from_customer',
                pk=customer.pk,
            )

        messages.success(
            request,
            f'Order {order.order_no} created for {customer.display_name}.',
        )
        try:
            return redirect(reverse('orders:order_detail', kwargs={'pk': order.pk}))
        except Exception:
            return redirect('customer_service_customer_detail', pk=customer.pk)

    @staticmethod
    def _source_choices():
        try:
            from apps.orders.models import OrderSource
            return list(OrderSource.choices)
        except Exception:
            return [
                ('walk_in', 'Walk-In'),
                ('phone',   'Phone Call'),
                ('email',   'Email'),
                ('other',   'Other'),
            ]

    @staticmethod
    def _collect_items(post) -> list:
        """
        Same item-parsing convention as CreateOrderFromTicketView — accepts
        either repeated `item_*` fields or indexed `items-N-*` fields.
        """
        items: list = []

        descs  = post.getlist('item_description') or post.getlist('item_description[]')
        qtys   = post.getlist('item_quantity')    or post.getlist('item_quantity[]')
        prices = post.getlist('item_unit_price')  or post.getlist('item_unit_price[]')
        prods  = post.getlist('item_product_id')  or post.getlist('item_product_id[]')

        for i, desc in enumerate(descs):
            d = (desc or '').strip()
            try:
                q = Decimal(qtys[i]) if i < len(qtys) and qtys[i] else Decimal('0')
            except Exception:
                q = Decimal('0')
            try:
                p = Decimal(prices[i]) if i < len(prices) and prices[i] else Decimal('0')
            except Exception:
                p = Decimal('0')
            if not d and q == 0:
                continue
            line = {'description': d, 'quantity': q, 'unit_price': p}
            if i < len(prods) and prods[i]:
                line['product_id'] = prods[i]
            items.append(line)

        if items:
            return items

        i = 0
        while True:
            desc = post.get(f'items-{i}-description')
            if desc is None:
                break
            d = desc.strip()
            try:
                q = Decimal(post.get(f'items-{i}-quantity', '0') or '0')
            except Exception:
                q = Decimal('0')
            try:
                p = Decimal(post.get(f'items-{i}-unit_price', '0') or '0')
            except Exception:
                p = Decimal('0')
            if not (not d and q == 0):
                line = {'description': d, 'quantity': q, 'unit_price': p}
                pid = post.get(f'items-{i}-product_id')
                if pid:
                    line['product_id'] = pid
                items.append(line)
            i += 1

        return items

# ════════════════════════════════════════════════════════════════════════
# 9. LIVE CHAT — toggle ON/OFF from the ticket detail page
#    Customer-facing chat page lives in a separate view that does NOT
#    inherit from CustomerServiceAccessMixin (anonymous tokenised access).
# ════════════════════════════════════════════════════════════════════════

class LiveChatToggleView(CustomerServiceAccessMixin, View):
    """
    POST /customer-service/tickets/<pk>/live-chat/toggle/

    Form fields:
        action: 'on' | 'off'   (required)

    On 'on': rotates the session token, emails the customer a fresh link.
    On 'off': deactivates immediately. No email.
    """
    http_method_names = ['post']

    def post(self, request, pk):
        ticket = get_object_or_404(ServiceTicket, pk=pk)
        action = (request.POST.get('action') or '').strip().lower()

        if ticket.status in {'closed', 'cancelled'} and action == 'on':
            messages.error(
                request,
                'You can’t open a live chat on a closed or cancelled ticket.',
            )
            return redirect('customer_service_ticket_detail', pk=ticket.pk)

        try:
            from . import live_chat_services as lcs
        except Exception:
            messages.error(request, 'Live chat is not available right now.')
            logger.exception('LiveChatToggleView: import failed')
            return redirect('customer_service_ticket_detail', pk=ticket.pk)

        try:
            if action == 'on':
                session = lcs.activate_session(ticket, actor=request.user, send_email=True)
                customer_email = ticket.customer.email or '(no email on file)'
                messages.success(
                    request,
                    f'Live chat opened. A fresh link has been emailed to '
                    f'{customer_email}. The link is bound to the first device '
                    f'that opens it.',
                )
            elif action == 'off':
                lcs.deactivate_session(ticket, actor=request.user, reason='manually_off')
                messages.success(
                    request,
                    'Live chat closed. The customer link no longer works.',
                )
            else:
                messages.error(request, 'Unknown live-chat action.')
        except Exception as exc:
            logger.exception('LiveChatToggleView failed for ticket %s', ticket.pk)
            messages.error(request, f'Could not toggle live chat: {exc}')

        return redirect('customer_service_ticket_detail', pk=ticket.pk)


class LiveChatResendInviteView(CustomerServiceAccessMixin, View):
    """
    POST /customer-service/tickets/<pk>/live-chat/resend/

    Resend the invite email without rotating the token. Useful if the
    customer says they didn't receive the email — saves them from a
    machine-rebind.
    """
    http_method_names = ['post']

    def post(self, request, pk):
        ticket = get_object_or_404(ServiceTicket, pk=pk)
        try:
            from . import live_chat_services as lcs
            session = lcs.get_or_create_session(ticket)
            if not session.is_active:
                messages.warning(
                    request,
                    'Live chat is currently OFF — turn it ON to send a fresh link.',
                )
                return redirect('customer_service_ticket_detail', pk=ticket.pk)
            lcs._send_invite_email(session)
            messages.success(
                request,
                f'Invite re-sent to {ticket.customer.email or "(no email)"}.',
            )
        except Exception as exc:
            logger.exception('LiveChatResendInviteView failed')
            messages.error(request, f'Could not resend invite: {exc}')
        return redirect('customer_service_ticket_detail', pk=ticket.pk)


class LiveChatCustomerStubView(View):
    """
    Customer-facing chat page (anonymous, tokenised).

    Pass 2: the REAL implementation. The class keeps its old name
    because urls_extra.py already references it; what matters is the
    URL name `cs_live_chat_customer`, which live_chat_services
    resolves via reverse(). If you'd rather rename the class, do it
    project-wide — there are only the two references.

    GET:
        1. Look up the session by token. Unknown → locked screen (404).
        2. Read the machine cookie. Bind / verify via
           live_chat_services.bind_first_visit().
        3. Usable → render live_chat_customer.html with the WS path
           baked in, set the cookie if newly issued.
        4. Locked / off / expired → render live_chat_locked.html.

    POST (no CSRF — tokenised endpoint, exempted at urls level):
        Called by the page once JS has computed the fingerprint. Re-runs
        bind_first_visit() with the fingerprint, which is where a hijack
        from a second machine gets caught.
    """
    http_method_names = ['get', 'post']

    # CSRF exempt — this endpoint is anonymous and tokenised. The token
    # itself is the credential.
    @classmethod
    def as_view(cls, **kwargs):
        from django.views.decorators.csrf import csrf_exempt
        view = super().as_view(**kwargs)
        return csrf_exempt(view)

    # ── GET ────────────────────────────────────────────────────────────
    def get(self, request, token):
        from django.shortcuts import render
        from .models import LiveChatSession
        from . import live_chat_services as lcs

        session = LiveChatSession.objects.select_related(
            'ticket', 'ticket__customer'
        ).filter(token=token).first()

        if session is None:
            return _render_locked(request, 'unknown')

        cookie_in = request.COOKIES.get(lcs.MACHINE_COOKIE_NAME, '')
        issue_new_cookie = False
        cookie_to_set = cookie_in

        if not session.machine_cookie:
            # First-ever visit — bind on cookie now; fingerprint finalised by POST.
            cookie_to_set = cookie_in or lcs.new_machine_cookie_value()
            issue_new_cookie = not cookie_in
            lcs.bind_first_visit(
                session,
                cookie_value=cookie_to_set,
                fingerprint_raw='',
                ip=_client_ip(request),
                user_agent=request.META.get('HTTP_USER_AGENT', ''),
            )
        else:
            # Returning visitor — verify cookie if present. If they
            # cleared cookies, fall through; the POST will try a
            # fingerprint match before declaring it a hijack.
            if cookie_in:
                ok, reason = lcs.bind_first_visit(
                    session,
                    cookie_value=cookie_in,
                    fingerprint_raw='',
                    ip=_client_ip(request),
                    user_agent=request.META.get('HTTP_USER_AGENT', ''),
                )
                if not ok:
                    return _render_locked(request, reason)

        ok, reason = lcs.session_is_usable(session)
        if not ok:
            return _render_locked(request, reason)

        ws_path = f'/ws/cs/live-chat/customer/{session.token}/'
        response = render(request, 'customer_service/live_chat_customer.html', {
            'session': session,
            'ticket': session.ticket,
            'customer': session.ticket.customer,
            'ws_path': ws_path,
            'finalize_url': request.path,
        })

        if issue_new_cookie:
            response.set_cookie(
                lcs.MACHINE_COOKIE_NAME,
                cookie_to_set,
                max_age=60 * 60 * 24 * 30,
                path=request.path,
                httponly=True,
                samesite='Lax',
                secure=request.is_secure(),
            )
        return response

    # ── POST — finalise fingerprint ────────────────────────────────────
    def post(self, request, token):
        from django.http import JsonResponse
        from .models import LiveChatSession
        from . import live_chat_services as lcs

        session = LiveChatSession.objects.filter(token=token).first()
        if session is None:
            return JsonResponse({'ok': False, 'reason': 'unknown'}, status=404)

        cookie_in = request.COOKIES.get(lcs.MACHINE_COOKIE_NAME, '')
        fp_raw = (request.POST.get('fingerprint') or '').strip()[:512]

        ok, reason = lcs.bind_first_visit(
            session,
            cookie_value=cookie_in,
            fingerprint_raw=fp_raw,
            ip=_client_ip(request),
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
        )
        return JsonResponse({'ok': ok, 'reason': reason})


# ── Local helpers for the live-chat customer view ──────────────────────

def _client_ip(request):
    """Best-effort client IP, trusting X-Forwarded-For if proxied."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '') or ''


_LOCK_LABELS = {
    'different_machine': 'This chat link has already been opened on a different device.',
    'expired':           'This chat link has expired.',
    'manually_off':      'The chat has been closed by our team.',
    'ticket_closed':     'The related ticket has been closed.',
    'unknown':           'This link is not recognised.',
}


def _render_locked(request, reason):
    from django.shortcuts import render
    return render(
        request,
        'customer_service/live_chat_locked.html',
        {
            'reason': reason or 'manually_off',
            'reason_label': _LOCK_LABELS.get(reason, 'This chat link is no longer active.'),
        },
        status=410 if reason in {'different_machine', 'expired', 'ticket_closed'} else
               404 if reason == 'unknown' else 200,
    )