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
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
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
            order = services.create_order_from_ticket(
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
        except Exception as exc:
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

    def post(self, request, pk):
        ticket = get_object_or_404(ServiceTicket, pk=pk)

        body = (request.POST.get('portal_body') or '').strip()
        if not body:
            messages.error(request, 'Reply body cannot be empty.')
            return redirect('customer_service_ticket_detail', pk=ticket.pk)

        portal_req = self._get_portal_request(ticket)
        if not portal_req:
            messages.error(request, 'No linked customer portal request found for this ticket.')
            return redirect('customer_service_ticket_detail', pk=ticket.pk)

        if portal_req.status in ('closed', 'cancelled'):
            messages.warning(
                request,
                f'The portal request is {portal_req.get_status_display().lower()} — reply not sent.',
            )
            return redirect('customer_service_ticket_detail', pk=ticket.pk)

        with transaction.atomic():
            from apps.customer_portal.models import TicketComment as PortalComment

            comment = PortalComment.objects.create(
                request=portal_req,
                author_kind='staff',
                author_user=request.user,
                author_display_name=(request.user.get_full_name() or request.user.username),
                body=body,
            )

            if portal_req.status == 'pending_review':
                portal_req.status = 'open'
                portal_req.save(update_fields=['status', 'updated_at'])

        transaction.on_commit(
            lambda: self._notify_customer(portal_req, comment, request.user)
        )

        messages.success(request, 'Reply sent to the customer portal.')
        return redirect('customer_service_ticket_detail', pk=ticket.pk)

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