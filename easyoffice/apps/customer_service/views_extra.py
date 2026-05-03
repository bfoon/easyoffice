"""
apps/customer_service/views_extra.py
────────────────────────────────────
New views to add on top of your existing customer_service.views.

They cover the four new flows:

    1. CreateOrderFromTicketView       — turns a ticket / call into a SalesOrder
    2. CompleteAssignmentView          — department-side "we've finished this"
    3. ConfirmAndCloseTicketView       — owner-side "customer confirmed, close it"
    4. HeadDashboardView + reports     — Head of CS / Head of Sales oversight

Wire them into urls.py as shown in the README header in `urls_extra.py`.
"""
from __future__ import annotations

import csv
from decimal import Decimal
from io import StringIO

from django.contrib import messages
from django.db.models import Count, Q, F, Avg, ExpressionWrapper, DurationField
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import View, TemplateView, DetailView

from .models import (
    ServiceTicket, ServiceTicketAssignment, ServiceTicketRouting,
    CallRecord, Customer, FeedbackRequest, ServiceRating,
)
from .permissions import (
    CustomerServiceAccessMixin, CustomerServiceOversightMixin,
    can_close_ticket, is_head_of_customer_service, is_head_of_sales,
)
from . import services


# ════════════════════════════════════════════════════════════════════════
# 1. CREATE ORDER FROM TICKET
# ════════════════════════════════════════════════════════════════════════

class CreateOrderFromTicketView(CustomerServiceAccessMixin, View):
    """
    POST /customer-service/tickets/<pk>/create-order/

    Form fields:
        currency, tax_rate, discount_amount, delivery_address, notes,
        items[] — repeatable: item_description, item_quantity, item_unit_price
                  (and optional item_product_id for inventory-linked lines)

    Returns: redirect to the new SalesOrder's detail page on success, or
    back to the ticket with a message on failure.
    """
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
        """
        Pull repeatable item_* fields out of the POST. Accepts two layouts:

          A) Parallel arrays: item_description[], item_quantity[], item_unit_price[]
          B) Indexed: items-0-description, items-0-quantity, items-0-unit_price, ...

        Lines with an empty description and zero quantity are skipped.
        """
        items: list = []

        # Layout A
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

        # Layout B
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
# 2. COMPLETE ASSIGNMENT  (department-side "we're done")
# ════════════════════════════════════════════════════════════════════════

class CompleteAssignmentView(CustomerServiceAccessMixin, View):
    """
    POST /customer-service/assignments/<pk>/complete/

    Allowed: the assignee, anyone in the assignment's department, or any
    supervisor / head. Triggers notify_assignment_completed which pings
    head of CS, the dept head, and the ticket creator/owner.
    """
    http_method_names = ['post']

    def post(self, request, pk):
        assignment = get_object_or_404(ServiceTicketAssignment, pk=pk)

        # Permission: assignee or someone in the same department, or supervisor
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

        messages.success(
            request,
            'Assignment marked complete. The ticket owner has been notified to confirm and close.',
        )
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
# 3. CONFIRM & CLOSE TICKET (owner-side)
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
                ticket, actor=request.user,
                confirmation_note=note, force=force,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect('customer_service_ticket_detail', pk=ticket.pk)

        messages.success(request, f'Ticket {ticket.ticket_no} closed.')
        return redirect('customer_service_ticket_detail', pk=ticket.pk)


# ════════════════════════════════════════════════════════════════════════
# 4. HEAD DASHBOARD + REPORTS
# ════════════════════════════════════════════════════════════════════════

class HeadDashboardView(CustomerServiceOversightMixin, TemplateView):
    """
    /customer-service/oversight/

    Single page, two audiences:
        • Head of CS — sees activity (calls, tickets, assignments, ratings)
        • Head of Sales — sees ticket pipeline + overdue + sales-type tickets

    Both heads can see the page; the template renders sections based on which
    flag is True. Superusers see everything.
    """
    template_name = 'customer_service/oversight.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        now = timezone.now()
        last_30 = now - timezone.timedelta(days=30)

        ctx['is_cs_head']    = is_head_of_customer_service(user) or user.is_superuser
        ctx['is_sales_head'] = is_head_of_sales(user)            or user.is_superuser

        tickets_qs = ServiceTicket.objects.all()

        # ── Top-line counts ─────────────────────────────────────────────
        ctx['totals'] = {
            'open': tickets_qs.exclude(status__in=('resolved', 'closed', 'cancelled')).count(),
            'overdue': tickets_qs.filter(
                resolution_due_at__lt=now,
            ).exclude(status__in=('resolved', 'closed', 'cancelled')).count(),
            'resolved_30d': tickets_qs.filter(
                resolved_at__gte=last_30,
            ).count(),
            'cancelled_30d': tickets_qs.filter(
                status='cancelled', updated_at__gte=last_30,
            ).count(),
            'calls_30d': CallRecord.objects.filter(started_at__gte=last_30).count(),
            'customers': Customer.objects.count(),
        }

        # ── Status breakdown ────────────────────────────────────────────
        ctx['status_breakdown'] = list(
            tickets_qs.values('status').annotate(n=Count('id')).order_by('-n')
        )

        # ── By department ───────────────────────────────────────────────
        ctx['by_department'] = list(
            tickets_qs.exclude(status__in=('closed', 'cancelled'))
            .values('department__name')
            .annotate(
                n=Count('id'),
                overdue=Count('id', filter=Q(resolution_due_at__lt=now)),
            )
            .order_by('-n')[:15]
        )

        # ── Overdue ticket list ─────────────────────────────────────────
        ctx['overdue_tickets'] = list(
            tickets_qs.filter(resolution_due_at__lt=now)
            .exclude(status__in=('resolved', 'closed', 'cancelled'))
            .select_related('customer', 'department', 'current_owner')
            .order_by('resolution_due_at')[:25]
        )

        # ── Recent CS activity timeline ─────────────────────────────────
        ctx['recent_calls'] = list(
            CallRecord.objects
            .select_related('customer', 'handled_by')
            .order_by('-started_at')[:15]
        )
        ctx['recent_tickets'] = list(
            tickets_qs.select_related('customer', 'department')
            .order_by('-created_at')[:15]
        )

        # ── Open assignments per department head can act on ─────────────
        ctx['stale_assignments'] = list(
            ServiceTicketAssignment.objects
            .filter(sla_due_at__lt=now)
            .exclude(status__in=('resolved', 'closed'))
            .select_related('ticket', 'department', 'assigned_to')
            .order_by('sla_due_at')[:20]
        )

        # ── Service ratings (CS head only — quality data) ───────────────
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
    """
    GET /customer-service/oversight/report.csv?from=YYYY-MM-DD&to=YYYY-MM-DD

    Streams a CSV report. Default range = last 30 days. The Head of CS gets
    the full dataset; Head of Sales gets ticket-only columns.
    """
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
            'Created At', 'Resolution Due', 'Resolved At', 'Closed At',
            'Overdue?',
        ]
        if is_cs_head:
            header += ['Call Linked', 'Callback Required', 'Feedback Sent', 'Avg Rating']
        w.writerow(header)

        for t in tickets:
            row = [
                t.ticket_no,
                t.subject,
                t.get_ticket_type_display(),
                t.get_priority_display(),
                t.get_status_display(),
                t.customer.display_name if t.customer_id else '',
                t.department.name if t.department_id else '',
                (t.created_by.get_full_name() if t.created_by_id else ''),
                (t.current_owner.get_full_name() if t.current_owner_id else ''),
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
                    parts = [
                        rating.service_quality or 0, rating.product_quality or 0,
                        rating.response_time or 0, rating.professionalism or 0,
                    ]
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
            f'attachment; filename="cs-report-'
            f'{date_from:%Y%m%d}-{date_to:%Y%m%d}.csv"'
        )
        return resp

    @staticmethod
    def _range(request):
        from datetime import datetime, time
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
