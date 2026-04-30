from datetime import timedelta
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.db.models import Count, Avg, Q
from django.db import transaction
from django.http import JsonResponse, HttpResponseForbidden, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.generic import TemplateView, DetailView, View
from django.core.mail import send_mail
from django.conf import settings

from apps.core.models import User, CoreNotification
from apps.organization.models import Department
from apps.staff.models import StaffProfile
from apps.tasks.models import Task, TaskCategory

from .models import (
    Customer,
    CustomerPhone,
    CallRecord,
    SLAPolicy,
    ServiceTicket,
    ServiceTicketRouting,
    ServiceTicketAssignment,
    ServiceTicketUpdate,
    FeedbackRequest,
    ServiceRating,
    normalize_phone,
)


# ── Role helpers ───────────────────────────────────────────────────────────

def _is_ceo(user):
    if user.is_superuser:
        return True
    if user.groups.filter(name__in=["CEO"]).exists():
        return True
    profile = getattr(user, "staffprofile", None)
    try:
        title = getattr(profile.position, "title", "") if profile and getattr(profile, "position", None) else ""
    except Exception:
        title = ""
    return str(title).strip().lower() == "ceo"


def _is_admin_like(user):
    return user.is_superuser or user.groups.filter(name__in=["Admin", "Office Manager"]).exists() or _is_ceo(user)


def _is_sales_rep(user):
    return _is_admin_like(user) or user.groups.filter(name__in=["Customer Service", "Sales Rep", "Sales"]).exists()


def _can_route_ticket(user, ticket):
    return _can_manage_department_ticket(user, ticket) or _is_sales_rep(user)

def _can_view_contacts(user):
    return _is_sales_rep(user) or _is_admin_like(user)


def _can_view_ticket(user, ticket):
    if _is_admin_like(user):
        return True
    profile = getattr(user, "staffprofile", None)
    dept = profile.department if profile else None
    if ticket.created_by_id == user.id or ticket.current_owner_id == user.id or ticket.resolved_by_id == user.id:
        return True
    if ticket.department and ticket.department.head_id == user.id:
        return True
    if dept and ticket.department_id == dept.id:
        return True
    if ticket.assignments.filter(assigned_to=user).exists():
        return True
    return False


def _can_manage_department_ticket(user, ticket):
    if _is_admin_like(user):
        return True
    profile = getattr(user, "staffprofile", None)
    dept = profile.department if profile else None
    if ticket.department and ticket.department.head_id == user.id:
        return True
    if dept and ticket.department_id == dept.id and getattr(profile, "is_supervisor_role", False):
        return True
    return False


def _notify(recipient, title, message, link="", sender=None):
    if not recipient:
        return
    try:
        CoreNotification.objects.create(
            recipient=recipient,
            sender=sender,
            notification_type=CoreNotification.Type.SYSTEM,
            title=title,
            message=message,
            link=link,
        )
    except Exception:
        pass

def _user_display_name(user):
    if not user:
        return "System"

    full_name = ""
    try:
        full_name = getattr(user, "full_name", "") or ""
    except Exception:
        full_name = ""

    if str(full_name).strip():
        return str(full_name).strip()

    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    fallback = f"{first} {last}".strip()
    if fallback:
        return fallback

    return getattr(user, "email", "") or getattr(user, "username", "") or "System"

def _send_service_email(subject, message, recipients):
    recipients = [r for r in recipients if r]
    if not recipients:
        return

    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=list(dict.fromkeys(recipients)),
            fail_silently=True,
        )
    except Exception:
        pass


def _department_users(department):
    if not department:
        return User.objects.none()

    return User.objects.filter(
        is_active=True,
        staffprofile__department=department,
    ).distinct()


def _department_emails(department, exclude_user_ids=None):
    exclude_user_ids = exclude_user_ids or []
    qs = _department_users(department).exclude(id__in=exclude_user_ids)
    return [u.email for u in qs if u.email]


def _send_assignment_email(ticket, assignee, sender, instructions="", request=None):
    if not assignee or not assignee.email:
        return

    if request:
        detail_url = request.build_absolute_uri(
            reverse("customer_service_ticket_detail", kwargs={"pk": ticket.pk})
        )
    else:
        detail_url = reverse("customer_service_ticket_detail", kwargs={"pk": ticket.pk})

    subject = f"Customer Service Assignment: {ticket.ticket_no}"

    message = f"""Hello {_user_display_name(assignee)},

You have been assigned a customer service ticket.

Ticket No: {ticket.ticket_no}
Customer: {ticket.customer.display_name}
Subject: {ticket.subject}
Priority: {ticket.get_priority_display()}
Department: {ticket.department.name if ticket.department else "—"}
Assigned By: {_user_display_name(sender)}
SLA Due: {ticket.resolution_due_at.strftime("%d %b %Y %H:%M") if ticket.resolution_due_at else "—"}

Instructions:
{instructions or ticket.description}

Open in EasyOffice:
{detail_url}
"""

    _send_service_email(subject, message, [assignee.email])


def _send_department_ticket_email(ticket, department, sender, note="", notify_all=False, request=None):
    if not department:
        return

    recipients = []

    if department.head and department.head.email:
        recipients.append(department.head.email)

    if notify_all:
        recipients.extend(_department_emails(department, exclude_user_ids=[sender.id]))

    recipients = list(dict.fromkeys([r for r in recipients if r]))
    if not recipients:
        return

    if request:
        detail_url = request.build_absolute_uri(
            reverse("customer_service_ticket_detail", kwargs={"pk": ticket.pk})
        )
    else:
        detail_url = reverse("customer_service_ticket_detail", kwargs={"pk": ticket.pk})

    subject = f"New Customer Service Ticket for {department.name}: {ticket.ticket_no}"

    message = f"""Hello {department.name} Team,

A customer service ticket has been routed to your department.

Ticket No: {ticket.ticket_no}
Customer: {ticket.customer.display_name}
Subject: {ticket.subject}
Type: {ticket.get_ticket_type_display()}
Priority: {ticket.get_priority_display()}
Created By: {_user_display_name(sender)}
SLA Due: {ticket.resolution_due_at.strftime("%d %b %Y %H:%M") if ticket.resolution_due_at else "—"}

Routing Note:
{note or ticket.description}

Open in EasyOffice:
{detail_url}
"""

    _send_service_email(subject, message, recipients)

def _send_feedback_email(ticket, request):
    target = ticket.callback_email or ticket.customer.email
    if not target:
        return None

    feedback, _ = FeedbackRequest.objects.get_or_create(
        ticket=ticket,
        defaults={
            "customer": ticket.customer,
            "channel": "email",
            "sent_to": target,
        },
    )
    feedback.sent_to = target
    feedback.sent_at = timezone.now()
    feedback.save(update_fields=["sent_to", "sent_at", "updated_at"])

    feedback_url = request.build_absolute_uri(
        reverse("customer_service_feedback", kwargs={"token": feedback.token})
    )

    try:
        send_mail(
            subject=f"How was our service for {ticket.ticket_no}?",
            message=(
                f"Dear {ticket.customer.display_name},\n\n"
                f"Your request {ticket.ticket_no} has been resolved.\n"
                f"We would appreciate your feedback on service quality and product quality.\n\n"
                f"Please rate your experience here:\n{feedback_url}\n\n"
                f"Thank you."
            ),
            from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
            recipient_list=[target],
            fail_silently=True,
        )
    except Exception:
        pass

    return feedback


def _sla_for(department, request_type, priority):
    if not department:
        return None
    return SLAPolicy.objects.filter(
        department=department,
        request_type=request_type,
        priority=priority,
        is_active=True,
    ).first()


def _apply_sla(ticket):
    policy = _sla_for(ticket.department, ticket.ticket_type, ticket.priority)
    if not policy:
        return
    base = timezone.now()
    ticket.response_due_at = base + timedelta(minutes=policy.first_response_minutes)
    ticket.resolution_due_at = base + timedelta(hours=policy.resolution_hours)
    ticket.save(update_fields=["response_due_at", "resolution_due_at", "updated_at"])


def _ensure_task_category(department):
    if not department:
        return None
    category, _ = TaskCategory.objects.get_or_create(
        name=f"Customer Service - {department.name}",
        defaults={
            "department": department,
            "icon": "bi-headset",
            "color": "#8b5cf6",
        },
    )
    return category


def _maybe_create_task(assignment, creator):
    if not assignment.assigned_to:
        return None
    try:
        category = _ensure_task_category(assignment.department)
        task = Task.objects.create(
            title=assignment.title,
            description=assignment.instructions or f"Customer service work item for {assignment.ticket.ticket_no}",
            assigned_to=assignment.assigned_to,
            assigned_by=creator,
            category=category,
            due_date=assignment.sla_due_at,
            priority={
                "low": Task.Priority.LOW,
                "medium": Task.Priority.MEDIUM,
                "high": Task.Priority.HIGH,
                "critical": Task.Priority.CRITICAL,
            }.get(assignment.ticket.priority, Task.Priority.MEDIUM),
        )
        assignment.task_ref = task
        assignment.save(update_fields=["task_ref", "updated_at"])
        return task
    except Exception:
        return None


def _customer_for_phone(phone_number):
    normalized = normalize_phone(phone_number)
    phone = CustomerPhone.objects.filter(normalized_number=normalized, is_active=True).select_related("customer").first()
    return phone.customer if phone else None


# ── Dashboard ─────────────────────────────────────────────────────────────

class CustomerServiceDashboardView(LoginRequiredMixin, TemplateView):
    template_name = "customer_service/dashboard.html"

    def dispatch(self, request, *args, **kwargs):
        if not _is_sales_rep(request.user):
            return HttpResponseForbidden("You do not have permission to view this dashboard.")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        today = timezone.localdate()

        tickets = ServiceTicket.objects.select_related("customer", "department", "current_owner")
        calls = CallRecord.objects.select_related("customer", "handled_by")

        ctx.update({
            "calls_today": calls.filter(started_at__date=today).count(),
            "open_tickets": tickets.exclude(status__in=["closed", "cancelled"]).count(),
            "overdue_tickets": len([
                t for t in tickets.exclude(status__in=["resolved", "closed", "cancelled"])
                if t.is_overdue
            ]),
            "escalated_tickets": tickets.filter(status="escalated").count(),
            "resolved_today": tickets.filter(resolved_at__date=today).count(),
            "recent_calls": calls.order_by("-started_at")[:8],
            "recent_tickets": tickets.order_by("-created_at")[:10],
            "tickets_by_department": Department.objects.filter(is_active=True).annotate(
                ticket_count=Count("customer_service_tickets")
            ).order_by("-ticket_count", "name")[:10],
            "avg_service_quality": ServiceRating.objects.aggregate(v=Avg("service_quality"))["v"],
            "avg_product_quality": ServiceRating.objects.aggregate(v=Avg("product_quality"))["v"],
        })
        return ctx


# ── Call desk ─────────────────────────────────────────────────────────────

class CallDeskView(LoginRequiredMixin, TemplateView):
    template_name = "customer_service/call_desk.html"

    def dispatch(self, request, *args, **kwargs):
        if not _is_sales_rep(request.user):
            return HttpResponseForbidden("You do not have permission to use the call desk.")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        profile = getattr(self.request.user, "staffprofile", None)

        dept_filter = Q(is_active=True)
        if profile and profile.department_id and not _is_admin_like(self.request.user):
            dept_filter &= (Q(id=profile.department_id) | Q(head=self.request.user))
        ctx.update({
            "departments": Department.objects.filter(dept_filter).order_by("name"),
            "recent_calls": CallRecord.objects.select_related("customer", "handled_by").order_by("-started_at")[:10],
            "recent_tickets": ServiceTicket.objects.select_related("customer", "department").order_by("-created_at")[:10],
        })
        return ctx

    def post(self, request, *args, **kwargs):
        phone_number = request.POST.get("phone_number", "").strip()
        subject = request.POST.get("subject", "").strip()
        summary = request.POST.get("summary", "").strip()
        call_type = request.POST.get("call_type", "general")
        outcome = request.POST.get("outcome", "").strip()
        priority = request.POST.get("priority", "medium")
        department_id = request.POST.get("department") or None
        create_ticket = request.POST.get("create_ticket") == "on"
        callback_required = request.POST.get("callback_required") == "on"
        callback_email = request.POST.get("callback_email", "").strip()
        callback_number = request.POST.get("callback_number", "").strip()

        if not phone_number:
            messages.error(request, "Phone number is required.")
            return render(request, self.template_name, self.get_context_data())

        if not subject:
            messages.error(request, "Subject is required.")
            return render(request, self.template_name, self.get_context_data())

        if not summary:
            messages.error(request, "Please write the call summary.")
            return render(request, self.template_name, self.get_context_data())

        customer = _customer_for_phone(phone_number)
        created_customer = False

        if not customer:
            full_name = request.POST.get("full_name", "").strip()
            if not full_name:
                messages.error(request, "New callers must have a customer name.")
                return render(request, self.template_name, self.get_context_data())

            customer = Customer.objects.create(
                full_name=full_name,
                company_name=request.POST.get("company_name", "").strip(),
                email=request.POST.get("email", "").strip(),
                address=request.POST.get("address", "").strip(),
                city=request.POST.get("city", "").strip(),
                area=request.POST.get("area", "").strip(),
                notes=request.POST.get("customer_notes", "").strip(),
                preferred_contact_method=request.POST.get("preferred_contact_method", "phone"),
                needs_feedback=request.POST.get("needs_feedback") == "on",
                created_by=request.user,
            )
            CustomerPhone.objects.create(
                customer=customer,
                phone_type="primary",
                phone_number=phone_number,
                is_primary=True,
            )
            created_customer = True

        department = Department.objects.filter(pk=department_id, is_active=True).first() if department_id else None

        call = CallRecord.objects.create(
            phone_number=phone_number,
            customer=customer,
            handled_by=request.user,
            call_type=call_type,
            subject=subject,
            summary=summary,
            outcome=outcome or ("resolved" if not create_ticket else "routed"),
            forwarded_to_department=department,
            callback_required=callback_required,
            callback_number=callback_number,
            callback_at=timezone.now() + timedelta(hours=4) if callback_required else None,
        )

        ticket = None
        if create_ticket:
            ticket = ServiceTicket.objects.create(
                customer=customer,
                call=call,
                ticket_type=call_type if call_type in dict(ServiceTicket.TICKET_TYPES) else "general",
                priority=priority if priority in dict(ServiceTicket.PRIORITIES) else "medium",
                status="new" if not department else "assigned",
                subject=subject,
                description=summary,
                department=department,
                created_by=request.user,
                callback_required=callback_required,
                callback_number=callback_number,
                callback_email=callback_email or customer.email,
                forwarded_call=(outcome == "forwarded"),
                requires_feedback=customer.needs_feedback,
            )
            _apply_sla(ticket)

            ServiceTicketUpdate.objects.create(
                ticket=ticket,
                user=request.user,
                update_type="note",
                body="Ticket created from call desk.",
            )

            if department:
                assignment = ServiceTicketAssignment.objects.create(
                    ticket=ticket,
                    department=department,
                    assigned_by=request.user,
                    assigned_to=department.head if department.head_id else None,
                    title=f"{ticket.ticket_no} - {ticket.subject}",
                    instructions=summary,
                    status="assigned" if department.head_id else "new",
                    sla_due_at=ticket.resolution_due_at,
                )
                _maybe_create_task(assignment, request.user)
                if department.head_id:
                    _notify(
                        department.head,
                        f"New customer service ticket {ticket.ticket_no}",
                        f"{customer.display_name} has a new {ticket.get_ticket_type_display().lower()} request.",
                        link=reverse("customer_service_ticket_detail", kwargs={"pk": ticket.pk}),
                        sender=request.user,
                    )
            if department and department != getattr(getattr(request.user, "staffprofile", None), "department", None):
                ServiceTicketRouting.objects.create(
                    ticket=ticket,
                    action="route",
                    from_department=getattr(getattr(request.user, "staffprofile", None), "department", None),
                    to_department=department,
                    from_user=request.user,
                    note="Initial routing from call desk.",
                )

            _send_department_ticket_email(
                ticket=ticket,
                department=department,
                sender=request.user,
                note=summary,
                notify_all=True,  # change to False if you only want the head
            )

        if created_customer:
            messages.success(request, f"New customer {customer.display_name} created and call logged.")
        elif ticket:
            messages.success(request, f"Call logged and ticket {ticket.ticket_no} created.")
        else:
            messages.success(request, "Call logged successfully.")

        if ticket:
            return redirect("customer_service_ticket_detail", pk=ticket.pk)
        return redirect("customer_service_call_desk")


class PhoneLookupView(LoginRequiredMixin, View):
    def get(self, request, *args, **kwargs):
        if not _is_sales_rep(request.user):
            return JsonResponse({"ok": False, "error": "Permission denied."}, status=403)

        phone = request.GET.get("phone", "").strip()
        if not phone:
            return JsonResponse({"ok": False, "error": "Phone is required."}, status=400)

        customer = _customer_for_phone(phone)
        if not customer:
            return JsonResponse({"ok": True, "exists": False, "normalized": normalize_phone(phone)})

        return JsonResponse({
            "ok": True,
            "exists": True,
            "normalized": normalize_phone(phone),
            "customer": {
                "id": str(customer.pk),
                "display_name": customer.display_name,
                "full_name": customer.full_name,
                "company_name": customer.company_name,
                "email": customer.email,
                "address": customer.address,
                "city": customer.city,
                "area": customer.area,
                "preferred_contact_method": customer.preferred_contact_method,
                "needs_feedback": customer.needs_feedback,
            },
            "phones": [
                {
                    "number": p.phone_number,
                    "type": p.get_phone_type_display(),
                    "primary": p.is_primary,
                }
                for p in customer.phones.filter(is_active=True)
            ],
            "recent_calls": [
                {
                    "id": c.id,
                    "subject": c.subject,
                    "call_type": c.get_call_type_display(),
                    "started_at": c.started_at.strftime("%d %b %Y %H:%M"),
                    "outcome": c.get_outcome_display() if c.outcome else "",
                }
                for c in customer.calls.all().order_by("-started_at")[:5]
            ],
            "open_tickets": [
                {
                    "id": str(t.pk),
                    "ticket_no": t.ticket_no,
                    "subject": t.subject,
                    "status": t.get_status_display(),
                    "department": t.department.name if t.department else "",
                }
                for t in customer.tickets.exclude(status__in=["closed", "cancelled"]).order_by("-created_at")[:5]
            ],
        })


# ── Customers ─────────────────────────────────────────────────────────────

class CustomerListView(LoginRequiredMixin, TemplateView):
    template_name = "customer_service/customer_list.html"

    def dispatch(self, request, *args, **kwargs):
        if not _can_view_contacts(request.user):
            return HttpResponseForbidden("You do not have permission to view contacts.")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "").strip()

        customers = Customer.objects.prefetch_related("phones").all()
        if q:
            customers = customers.filter(
                Q(full_name__icontains=q) |
                Q(company_name__icontains=q) |
                Q(email__icontains=q) |
                Q(customer_code__icontains=q) |
                Q(phones__phone_number__icontains=q)
            ).distinct()

        ctx["customers"] = customers.order_by("full_name")[:200]
        ctx["q"] = q
        return ctx


class CustomerDetailView(LoginRequiredMixin, DetailView):
    model = Customer
    template_name = "customer_service/customer_detail.html"
    context_object_name = "customer"

    def dispatch(self, request, *args, **kwargs):
        if not _can_view_contacts(request.user):
            return HttpResponseForbidden("You do not have permission to view contacts.")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        customer = self.get_object()
        ctx["phones"] = customer.phones.filter(is_active=True)
        ctx["calls"] = customer.calls.select_related("handled_by").order_by("-started_at")[:20]
        ctx["tickets"] = customer.tickets.select_related("department", "current_owner").order_by("-created_at")[:20]
        ctx["ratings"] = customer.ratings.order_by("-created_at")[:10]
        return ctx


# ── Tickets ───────────────────────────────────────────────────────────────

class TicketListView(LoginRequiredMixin, TemplateView):
    template_name = "customer_service/ticket_list.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "").strip()
        dept = self.request.GET.get("department", "").strip()
        mine = self.request.GET.get("mine", "").strip()

        tickets = ServiceTicket.objects.select_related("customer", "department", "current_owner", "created_by")

        if not _is_admin_like(self.request.user):
            profile = getattr(self.request.user, "staffprofile", None)
            dept_id = profile.department_id if profile else None
            tickets = tickets.filter(
                Q(created_by=self.request.user) |
                Q(current_owner=self.request.user) |
                Q(assignments__assigned_to=self.request.user) |
                Q(department__head=self.request.user) |
                Q(department_id=dept_id)
            ).distinct()

        if q:
            tickets = tickets.filter(
                Q(ticket_no__icontains=q) |
                Q(subject__icontains=q) |
                Q(customer__full_name__icontains=q) |
                Q(customer__company_name__icontains=q)
            )
        if status:
            tickets = tickets.filter(status=status)
        if dept:
            tickets = tickets.filter(department_id=dept)
        if mine == "1":
            tickets = tickets.filter(
                Q(created_by=self.request.user) |
                Q(current_owner=self.request.user) |
                Q(assignments__assigned_to=self.request.user)
            ).distinct()

        ctx.update({
            "tickets": tickets.order_by("-created_at")[:200],
            "departments": Department.objects.filter(is_active=True).order_by("name"),
            "q": q,
            "status_filter": status,
            "department_filter": dept,
            "mine_filter": mine,
            "status_choices": ServiceTicket.STATUSES,
        })
        return ctx


class TicketDetailView(LoginRequiredMixin, DetailView):
    model = ServiceTicket
    template_name = "customer_service/ticket_detail.html"
    context_object_name = "ticket"

    def dispatch(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not _can_view_ticket(request.user, self.object):
            return HttpResponseForbidden("You do not have permission to view this ticket.")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ticket = self.object

        if ticket.department:
            dept_users = User.objects.filter(
                is_active=True,
                staffprofile__department=ticket.department,
            ).select_related("staffprofile").order_by("first_name", "last_name")
        else:
            dept_users = User.objects.filter(
                is_active=True,
                staffprofile__department__isnull=False,
            ).select_related("staffprofile", "staffprofile__department").order_by("first_name", "last_name")

        ctx.update({
            "updates": ticket.updates.select_related("user"),
            "routes": ticket.routes.select_related("from_department", "to_department", "from_user", "to_user"),
            "assignments": ticket.assignments.select_related("department", "assigned_by", "assigned_to", "task_ref"),
            "dept_users": dept_users,
            "departments": Department.objects.filter(is_active=True).order_by("name"),
            "can_manage_department": _can_manage_department_ticket(self.request.user, ticket),
            "can_route_ticket": _can_route_ticket(self.request.user, ticket),
            "can_send_feedback": _is_admin_like(self.request.user) or ticket.created_by_id == self.request.user.id,
        })
        return ctx


class TicketActionView(View):
    def post(self, request, pk):
        ticket = get_object_or_404(ServiceTicket, pk=pk)
        action = request.POST.get("action")

        if action == "assign":
            department = None
            assigned_to = None

            department_id = request.POST.get("department")
            assigned_to_id = request.POST.get("assigned_to")

            # get department from POST first
            if department_id:
                from apps.organization.models import Department
                department = Department.objects.filter(pk=department_id).first()

            # fallback to ticket.department
            if not department:
                department = ticket.department

            # hard stop instead of IntegrityError
            if not department:
                messages.error(request, "Please select a department before assigning this ticket.")
                return redirect("customer_service_ticket_detail", pk=ticket.pk)

            if assigned_to_id:
                from django.contrib.auth import get_user_model
                User = get_user_model()
                assigned_to = User.objects.filter(pk=assigned_to_id).first()

            with transaction.atomic():
                # keep ticket and assignment aligned
                ticket.department = department
                ticket.current_owner = assigned_to or ticket.current_owner
                ticket.status = "assigned"
                ticket.save()

                ServiceTicketAssignment.objects.create(
                    ticket=ticket,
                    department=department,
                    assigned_by=request.user,
                    assigned_to=assigned_to,
                    title=f"{ticket.ticket_no} - {ticket.subject}",
                    instructions=ticket.description or "",
                    status="assigned",
                    sla_due_at=ticket.resolution_due_at,
                )

                ServiceTicketUpdate.objects.create(
                    ticket=ticket,
                    user=request.user,
                    update_type="status_change",
                    body=f"Ticket assigned to {department}" + (
                        f" and {assigned_to}" if assigned_to else ""
                    ),
                )

            messages.success(request, "Ticket assigned successfully.")
            return redirect("customer_service_ticket_detail", pk=ticket.pk)

        messages.error(request, "Invalid action.")
        return redirect("customer_service_ticket_detail", pk=ticket.pk)


# ── Department queue ──────────────────────────────────────────────────────

class DepartmentQueueView(LoginRequiredMixin, TemplateView):
    template_name = "customer_service/department_queue.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        profile = getattr(user, "staffprofile", None)
        dept = profile.department if profile else None

        assignments = ServiceTicketAssignment.objects.select_related(
            "ticket", "department", "assigned_by", "assigned_to", "task_ref", "ticket__customer"
        )

        if _is_admin_like(user):
            pass
        elif dept and getattr(profile, "is_supervisor_role", False):
            assignments = assignments.filter(department=dept)
        elif dept and dept.head_id == user.id:
            assignments = assignments.filter(department=dept)
        else:
            assignments = assignments.filter(assigned_to=user)

        ctx.update({
            "assignments": assignments.order_by("-created_at")[:200],
            "my_department": dept,
        })
        return ctx


# ── Public feedback ───────────────────────────────────────────────────────

class FeedbackFormView(View):
    template_name = "customer_service/feedback_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.feedback = get_object_or_404(FeedbackRequest, token=kwargs["token"])
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        if self.feedback.completed_at:
            return render(request, self.template_name, {
                "feedback": self.feedback,
                "submitted": True,
            })
        return render(request, self.template_name, {
            "feedback": self.feedback,
            "submitted": False,
        })

    def post(self, request, *args, **kwargs):
        if self.feedback.completed_at:
            return render(request, self.template_name, {
                "feedback": self.feedback,
                "submitted": True,
            })

        def score(name):
            try:
                return max(1, min(5, int(request.POST.get(name, "0"))))
            except Exception:
                return 0

        service_quality = score("service_quality")
        product_quality = score("product_quality")
        response_time = score("response_time")
        professionalism = score("professionalism")
        comment = request.POST.get("comment", "").strip()

        if not all([service_quality, product_quality, response_time, professionalism]):
            return render(request, self.template_name, {
                "feedback": self.feedback,
                "submitted": False,
                "error": "Please rate all fields before submitting.",
            })

        ServiceRating.objects.update_or_create(
            ticket=self.feedback.ticket,
            defaults={
                "customer": self.feedback.customer,
                "service_quality": service_quality,
                "product_quality": product_quality,
                "response_time": response_time,
                "professionalism": professionalism,
                "comment": comment,
            }
        )
        self.feedback.completed_at = timezone.now()
        self.feedback.save(update_fields=["completed_at", "updated_at"])

        return render(request, self.template_name, {
            "feedback": self.feedback,
            "submitted": True,
        })