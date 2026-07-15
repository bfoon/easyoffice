from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, DetailView, View
from django.urls import reverse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.utils import timezone
from django.db.models import Q, Count
from apps.it_support.models import (
    ITRequest, Equipment, TicketComment, MaintenanceJob,
    MaintenanceChargeLine, MaintenanceEvent, MaintenanceAttachment,
    MaintenanceAccessToken, MaintenanceReminder,
)
from apps.core.models import User


def _is_it_staff(user):
    return user.is_superuser or user.groups.filter(name='IT').exists()


def _gen_ticket_number():
    try:
        import shortuuid
        return f'IT-{shortuuid.ShortUUID().random(length=6).upper()}'
    except ImportError:
        import random, string
        return 'IT-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _notify_it(ticket, sender):
    """Send in-app notification to all IT group members."""
    try:
        from apps.core.models import CoreNotification
        it_staff = User.objects.filter(groups__name='IT', is_active=True)
        for u in it_staff:
            CoreNotification.objects.create(
                recipient=u, sender=sender,
                notification_type='it',
                title=f'New IT Ticket: {ticket.ticket_number}',
                message=f'{sender.full_name} raised: {ticket.title}',
                link=f'/it/tickets/{ticket.id}/',
            )
    except Exception:
        pass


def _notify_requester(ticket, actor, title, msg):
    try:
        from apps.core.models import CoreNotification
        CoreNotification.objects.create(
            recipient=ticket.requested_by, sender=actor,
            notification_type='it',
            title=title, message=msg,
            link=f'/it/tickets/{ticket.id}/',
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class ITDashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'it_support/dashboard.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        is_it = _is_it_staff(user)
        ctx['is_it'] = is_it

        if is_it:
            all_tickets = ITRequest.objects.all()
            ctx.update({
                'open_tickets': all_tickets.filter(
                    status__in=['open', 'in_progress', 'pending_info']
                ).select_related('requested_by', 'assigned_to').order_by('-created_at')[:12],
                'open_count': all_tickets.filter(status='open').count(),
                'in_progress_count': all_tickets.filter(status='in_progress').count(),
                'pending_count': all_tickets.filter(status='pending_info').count(),
                'resolved_today': all_tickets.filter(
                    status='resolved', resolved_at__date=timezone.now().date()
                ).count(),
                'critical_count': all_tickets.filter(
                    status__in=['open', 'in_progress'], priority='critical'
                ).count(),
                'my_assigned': all_tickets.filter(
                    assigned_to=user, status__in=['open', 'in_progress']
                ).select_related('requested_by').order_by('-created_at')[:5],
                'all_equipment': Equipment.objects.select_related('current_user').order_by('status', 'name')[:20],
                'equipment_stats': {
                    'available': Equipment.objects.filter(status='available').count(),
                    'issued': Equipment.objects.filter(status='issued').count(),
                    'maintenance': Equipment.objects.filter(status='maintenance').count(),
                },
                'maintenance_stats': {
                    'active': MaintenanceJob.objects.exclude(
                        status__in=['closed', 'cancelled', 'unrepairable']
                    ).count(),
                    'overdue': sum(
                        1 for j in MaintenanceJob.objects.exclude(
                            status__in=['ready_pickup', 'collected', 'closed', 'cancelled', 'unrepairable']
                        ).filter(promised_completion_at__isnull=False)
                        if j.is_overdue
                    ),
                    'ready_pickup': MaintenanceJob.objects.filter(status='ready_pickup').count(),
                    'pickup_overdue': sum(
                        1 for j in MaintenanceJob.objects.filter(
                            status='ready_pickup', pickup_deadline_at__isnull=False
                        ) if j.pickup_is_overdue
                    ),
                },
                'maintenance_queue': MaintenanceJob.objects.select_related(
                    'assigned_to', 'owner_user', 'equipment'
                ).exclude(status__in=['closed', 'cancelled']).order_by(
                    'promised_completion_at', '-priority'
                )[:8],
                'staff_list': User.objects.filter(
                    is_active=True, status='active'
                ).order_by('first_name'),
            })
        else:
            my_tickets = ITRequest.objects.filter(requested_by=user)
            ctx.update({
                'my_tickets': my_tickets.select_related('assigned_to').order_by('-created_at')[:10],
                'open_count': my_tickets.filter(status__in=['open', 'in_progress']).count(),
                'resolved_count': my_tickets.filter(status='resolved').count(),
                'my_equipment': Equipment.objects.filter(current_user=user),
                'type_choices': ITRequest.Type.choices,
                'priority_choices': ITRequest.Priority.choices,
            })

        return ctx


# ---------------------------------------------------------------------------
# Ticket List
# ---------------------------------------------------------------------------

class TicketListView(LoginRequiredMixin, TemplateView):
    template_name = 'it_support/ticket_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        is_it = _is_it_staff(user)

        q = self.request.GET.get('q', '')
        status = self.request.GET.get('status', '')
        rtype = self.request.GET.get('type', '')
        priority = self.request.GET.get('priority', '')

        tickets = ITRequest.objects.all() if is_it else ITRequest.objects.filter(requested_by=user)

        if q:
            tickets = tickets.filter(
                Q(ticket_number__icontains=q) | Q(title__icontains=q) |
                Q(description__icontains=q)
            )
        if status:
            tickets = tickets.filter(status=status)
        if rtype:
            tickets = tickets.filter(request_type=rtype)
        if priority:
            tickets = tickets.filter(priority=priority)

        ctx.update({
            'tickets': tickets.select_related('requested_by', 'assigned_to').order_by('-created_at'),
            'status_choices': ITRequest.Status.choices,
            'type_choices': ITRequest.Type.choices,
            'priority_choices': ITRequest.Priority.choices,
            'is_it': is_it,
            'q': q,
            'status_filter': status,
            'type_filter': rtype,
            'priority_filter': priority,
            'total': tickets.count(),
        })
        return ctx


# ---------------------------------------------------------------------------
# Ticket Detail
# ---------------------------------------------------------------------------

class TicketDetailView(LoginRequiredMixin, View):
    template_name = 'it_support/ticket_detail.html'

    def get(self, request, pk):
        ticket = get_object_or_404(ITRequest, pk=pk)
        is_it = _is_it_staff(request.user)

        # Access: requester, IT staff, or assignee
        if not (is_it or ticket.requested_by == request.user):
            messages.error(request, 'You do not have access to this ticket.')
            return redirect('ticket_list')

        comments = ticket.comments.select_related('author').order_by('created_at')
        if not is_it:
            comments = comments.filter(is_internal=False)

        ctx = {
            'ticket': ticket,
            'comments': comments,
            'is_it': is_it,
            'status_choices': ITRequest.Status.choices,
            'staff_list': User.objects.filter(
                groups__name='IT', is_active=True
            ).order_by('first_name') if is_it else [],
        }
        return render(request, self.template_name, ctx)

    def post(self, request, pk):
        ticket = get_object_or_404(ITRequest, pk=pk)
        is_it = _is_it_staff(request.user)

        if not (is_it or ticket.requested_by == request.user):
            messages.error(request, 'Permission denied.')
            return redirect('ticket_list')

        action = request.POST.get('action', 'comment')

        if action == 'comment':
            content = request.POST.get('content', '').strip()
            if content:
                is_internal = request.POST.get('is_internal') == 'on' and is_it
                TicketComment.objects.create(
                    ticket=ticket, author=request.user,
                    content=content, is_internal=is_internal,
                )
                if not is_internal and request.user != ticket.requested_by:
                    _notify_requester(
                        ticket, request.user,
                        f'Reply on {ticket.ticket_number}',
                        f'{request.user.full_name} replied to your ticket.',
                    )

        elif action == 'assign' and is_it:
            assignee_id = request.POST.get('assignee_id')
            if assignee_id:
                try:
                    ticket.assigned_to = User.objects.get(id=assignee_id)
                except User.DoesNotExist:
                    pass
            else:
                ticket.assigned_to = request.user
            if ticket.status == 'open':
                ticket.status = 'in_progress'
            ticket.save(update_fields=['assigned_to', 'status', 'updated_at'])
            messages.success(request, 'Ticket assigned.')

        elif action == 'status' and is_it:
            new_status = request.POST.get('status')
            if new_status:
                ticket.status = new_status
                if new_status == 'resolved':
                    ticket.resolution = request.POST.get('resolution', '')
                    ticket.resolved_at = timezone.now()
                    _notify_requester(
                        ticket, request.user,
                        f'Ticket {ticket.ticket_number} Resolved',
                        f'Your IT ticket "{ticket.title}" has been resolved.',
                    )
                ticket.save()
            messages.success(request, 'Status updated.')

        elif action == 'close' and ticket.requested_by == request.user:
            if ticket.status == 'resolved':
                ticket.status = 'closed'
                ticket.save(update_fields=['status', 'updated_at'])
                messages.success(request, 'Ticket closed. Thanks for confirming.')

        return redirect('ticket_detail', pk=pk)


# ---------------------------------------------------------------------------
# Create Ticket
# ---------------------------------------------------------------------------

class TicketCreateView(LoginRequiredMixin, View):
    template_name = 'it_support/ticket_form.html'

    def get(self, request):
        return render(request, self.template_name, {
            'type_choices': ITRequest.Type.choices,
            'priority_choices': ITRequest.Priority.choices,
        })

    def post(self, request):
        ticket = ITRequest.objects.create(
            ticket_number=_gen_ticket_number(),
            request_type=request.POST['request_type'],
            title=request.POST['title'],
            description=request.POST['description'],
            requested_by=request.user,
            priority=request.POST.get('priority', 'normal'),
            sla_hours={'critical': 4, 'high': 8, 'normal': 24, 'low': 72}.get(
                request.POST.get('priority', 'normal'), 24
            ),
        )
        if 'attachment' in request.FILES:
            ticket.attachment = request.FILES['attachment']
            ticket.save(update_fields=['attachment'])

        _notify_it(ticket, request.user)
        messages.success(request, f'Ticket {ticket.ticket_number} submitted. We\'ll be in touch soon.')
        return redirect('ticket_detail', pk=ticket.id)


# ---------------------------------------------------------------------------
# Ticket Update (legacy — kept for backwards compat, redirects to detail)
# ---------------------------------------------------------------------------

class TicketUpdateView(LoginRequiredMixin, View):
    def post(self, request, pk):
        ticket = get_object_or_404(ITRequest, pk=pk)
        is_it = _is_it_staff(request.user)
        action = request.POST.get('action')

        if action == 'assign' and is_it:
            ticket.assigned_to = request.user
            ticket.status = 'in_progress'
            ticket.save(update_fields=['assigned_to', 'status', 'updated_at'])

        elif action == 'resolve' and is_it:
            ticket.status = 'resolved'
            ticket.resolution = request.POST.get('resolution', '')
            ticket.resolved_at = timezone.now()
            ticket.save()
            _notify_requester(
                ticket, request.user,
                f'Ticket {ticket.ticket_number} Resolved',
                f'Your IT ticket "{ticket.title}" has been resolved.',
            )

        messages.success(request, 'Ticket updated.')
        return redirect('ticket_detail', pk=pk)


# ---------------------------------------------------------------------------
# Equipment List (IT staff only)
# ---------------------------------------------------------------------------

class EquipmentListView(LoginRequiredMixin, TemplateView):
    template_name = 'it_support/equipment_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        if not _is_it_staff(self.request.user):
            return ctx

        q = self.request.GET.get('q', '')
        status = self.request.GET.get('status', '')
        category = self.request.GET.get('category', '')

        equipment = Equipment.objects.select_related('current_user').order_by('status', 'name')
        if q:
            equipment = equipment.filter(
                Q(asset_tag__icontains=q) | Q(name__icontains=q) |
                Q(serial_number__icontains=q) | Q(make__icontains=q)
            )
        if status:
            equipment = equipment.filter(status=status)
        if category:
            equipment = equipment.filter(category__icontains=category)

        categories = Equipment.objects.values_list('category', flat=True).distinct().order_by('category')

        ctx.update({
            'equipment': equipment,
            'status_choices': Equipment.Status.choices,
            'categories': categories,
            'staff_list': User.objects.filter(
                is_active=True, status='active'
            ).order_by('first_name'),
            'is_it': True,
            'q': q,
            'status_filter': status,
            'category_filter': category,
            'stats': {
                'available': Equipment.objects.filter(status='available').count(),
                'issued': Equipment.objects.filter(status='issued').count(),
                'maintenance': Equipment.objects.filter(status='maintenance').count(),
                'total': Equipment.objects.count(),
            },
        })
        return ctx


# ---------------------------------------------------------------------------
# Equipment Action (issue / return / status update)
# ---------------------------------------------------------------------------

class EquipmentActionView(LoginRequiredMixin, View):
    def post(self, request, pk):
        if not _is_it_staff(request.user):
            messages.error(request, 'IT staff only.')
            return redirect('it_dashboard')

        equipment = get_object_or_404(Equipment, pk=pk)
        action = request.POST.get('action')

        if action == 'issue':
            user_id = request.POST.get('user_id')
            if not user_id:
                messages.error(request, 'Please select a staff member.')
                return redirect('equipment_list')
            try:
                target_user = User.objects.get(id=user_id)
                equipment.current_user = target_user
                equipment.status = Equipment.Status.ISSUED
                equipment.issued_date = timezone.now().date()
                equipment.save()
                messages.success(request, f'{equipment.name} issued to {target_user.full_name}.')
            except User.DoesNotExist:
                messages.error(request, 'User not found.')

        elif action == 'return':
            equipment.current_user = None
            equipment.status = Equipment.Status.AVAILABLE
            equipment.issued_date = None
            equipment.save()
            messages.success(request, f'{equipment.name} returned and marked available.')

        elif action == 'status':
            new_status = request.POST.get('status')
            if new_status in dict(Equipment.Status.choices):
                equipment.status = new_status
                # Maintenance must not erase the equipment owner/assignee.
                # Clear assignment only when the asset truly leaves issued use.
                if new_status in ('available', 'retired', 'lost'):
                    equipment.current_user = None
                    equipment.issued_date = None
                equipment.save()
                messages.success(request, f'{equipment.name} status updated to {new_status}.')

        next_url = request.POST.get('next', '')
        if next_url and next_url.startswith('/'):
            return redirect(next_url)
        return redirect('equipment_list')
# =============================================================================
# Equipment Maintenance Management
# =============================================================================

from django.utils.dateparse import parse_datetime
from apps.it_support import maintenance_services


def _require_it(request):
    if not _is_it_staff(request.user):
        messages.error(request, 'IT staff only.')
        return False
    return True


def _parse_local_datetime(value):
    value = (value or '').strip()
    if not value:
        return None
    dt = parse_datetime(value)
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


class MaintenanceListView(LoginRequiredMixin, TemplateView):
    template_name = 'it_support/maintenance_list.html'

    def dispatch(self, request, *args, **kwargs):
        if not _is_it_staff(request.user):
            messages.error(request, 'IT staff only.')
            return redirect('it_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        q = self.request.GET.get('q', '').strip()
        status = self.request.GET.get('status', '').strip()
        priority = self.request.GET.get('priority', '').strip()
        technician = self.request.GET.get('technician', '').strip()
        view = self.request.GET.get('view', '').strip()

        jobs = MaintenanceJob.objects.select_related(
            'assigned_to', 'owner_user', 'equipment', 'invoice', 'receipt'
        )
        if q:
            jobs = jobs.filter(
                Q(maintenance_number__icontains=q)
                | Q(equipment_name__icontains=q)
                | Q(serial_number__icontains=q)
                | Q(asset_tag__icontains=q)
                | Q(owner_name__icontains=q)
                | Q(owner_email__icontains=q)
                | Q(owner_phone__icontains=q)
            )
        if status:
            jobs = jobs.filter(status=status)
        if priority:
            jobs = jobs.filter(priority=priority)
        if technician:
            jobs = jobs.filter(assigned_to_id=technician)
        if view == 'active':
            jobs = jobs.exclude(status__in=['closed', 'cancelled', 'unrepairable'])
        elif view == 'ready':
            jobs = jobs.filter(status=MaintenanceJob.Status.READY_PICKUP)
        elif view == 'overdue':
            jobs = jobs.filter(
                promised_completion_at__lt=timezone.now()
            ).exclude(status__in=[
                MaintenanceJob.Status.READY_PICKUP,
                MaintenanceJob.Status.COLLECTED,
                MaintenanceJob.Status.CLOSED,
                MaintenanceJob.Status.CANCELLED,
                MaintenanceJob.Status.UNREPAIRABLE,
            ])
        elif view == 'pickup_overdue':
            jobs = jobs.filter(
                status=MaintenanceJob.Status.READY_PICKUP,
                pickup_deadline_at__lt=timezone.now(),
            )

        all_jobs = MaintenanceJob.objects.all()
        ctx.update({
            'jobs': jobs.order_by('-created_at'),
            'status_choices': MaintenanceJob.Status.choices,
            'priority_choices': MaintenanceJob.Priority.choices,
            'technicians': User.objects.filter(
                Q(groups__name='IT') | Q(groups__name='Technician'),
                is_active=True,
            ).distinct().order_by('first_name', 'last_name'),
            'q': q,
            'status_filter': status,
            'priority_filter': priority,
            'technician_filter': technician,
            'view_filter': view,
            'stats': {
                'total': all_jobs.count(),
                'active': all_jobs.exclude(status__in=['closed', 'cancelled', 'unrepairable']).count(),
                'ready': all_jobs.filter(status='ready_pickup').count(),
                'overdue': all_jobs.filter(
                    promised_completion_at__lt=timezone.now()
                ).exclude(status__in=['ready_pickup', 'collected', 'closed', 'cancelled', 'unrepairable']).count(),
                'pickup_overdue': all_jobs.filter(
                    status='ready_pickup', pickup_deadline_at__lt=timezone.now()
                ).count(),
            },
        })
        return ctx


class MaintenanceCreateView(LoginRequiredMixin, View):
    template_name = 'it_support/maintenance_form.html'

    def dispatch(self, request, *args, **kwargs):
        if not _is_it_staff(request.user):
            messages.error(request, 'IT staff only.')
            return redirect('it_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def _context(self, data=None):
        return {
            'equipment_list': Equipment.objects.select_related('current_user').order_by('name'),
            'owners': User.objects.filter(is_active=True).order_by('first_name', 'last_name'),
            'technicians': User.objects.filter(
                Q(groups__name='IT') | Q(groups__name='Technician'),
                is_active=True,
            ).distinct().order_by('first_name', 'last_name'),
            'priority_choices': MaintenanceJob.Priority.choices,
            'intake_choices': MaintenanceJob.IntakeChannel.choices,
            'data': data or {},
        }

    def get(self, request):
        return render(request, self.template_name, self._context())

    def post(self, request):
        payload = request.POST.dict()
        payload['data_privacy_consent'] = request.POST.get('data_privacy_consent') == 'on'
        payload['backup_authorized'] = request.POST.get('backup_authorized') == 'on'
        payload['diagnosis_due_at'] = _parse_local_datetime(request.POST.get('diagnosis_due_at'))
        payload['promised_completion_at'] = _parse_local_datetime(
            request.POST.get('promised_completion_at')
        )
        try:
            job = maintenance_services.create_maintenance_job(
                payload, actor=request.user, request=request
            )
            if request.FILES.get('intake_file'):
                attachment = MaintenanceAttachment.objects.create(
                    job=job,
                    category=MaintenanceAttachment.Category.INTAKE,
                    file=request.FILES['intake_file'],
                    caption=request.POST.get('intake_caption', ''),
                    is_public=request.POST.get('intake_file_public') == 'on',
                    uploaded_by=request.user,
                )
                MaintenanceEvent.objects.create(
                    job=job,
                    kind=MaintenanceEvent.Kind.ATTACHMENT,
                    actor=request.user,
                    message=f'Intake attachment added: {attachment.file.name}.',
                    is_public=attachment.is_public,
                )
            messages.success(
                request,
                f'{job.maintenance_number} created. The owner tracking link was sent when an email was available.'
            )
            return redirect('maintenance_detail', pk=job.pk)
        except Exception as exc:
            messages.error(request, str(exc))
            return render(request, self.template_name, self._context(request.POST))


class MaintenanceDetailView(LoginRequiredMixin, TemplateView):
    template_name = 'it_support/maintenance_detail.html'

    def dispatch(self, request, *args, **kwargs):
        if not _is_it_staff(request.user):
            messages.error(request, 'IT staff only.')
            return redirect('it_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        job = get_object_or_404(
            MaintenanceJob.objects.select_related(
                'equipment', 'owner_user', 'created_by', 'assigned_to',
                'invoice', 'receipt', 'invoice__generated_pdf', 'receipt__generated_pdf'
            ).prefetch_related('charge_lines', 'events__actor', 'attachments', 'reminders'),
            pk=kwargs['pk'],
        )
        token, _ = MaintenanceAccessToken.objects.get_or_create(job=job)
        ctx.update({
            'job': job,
            'tracking_url': request.build_absolute_uri(
                reverse('maintenance_public', kwargs={'token': token.token})
            ),
            'events': job.events.select_related('actor').order_by('-created_at'),
            'public_events': job.events.filter(is_public=True).order_by('created_at'),
            'charge_lines': job.charge_lines.all(),
            'attachments': job.attachments.select_related('uploaded_by'),
            'reminders': job.reminders.all(),
            'technicians': User.objects.filter(
                Q(groups__name='IT') | Q(groups__name='Technician'),
                is_active=True,
            ).distinct().order_by('first_name', 'last_name'),
            'status_choices': MaintenanceJob.Status.choices,
            'charge_type_choices': MaintenanceChargeLine.ChargeType.choices,
            'attachment_categories': MaintenanceAttachment.Category.choices,
            'allowed_next_statuses': [
                (value, dict(MaintenanceJob.Status.choices)[value])
                for value in maintenance_services.ALLOWED_TRANSITIONS.get(job.status, set())
            ],
        })
        return ctx


class MaintenanceActionView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        if not _require_it(request):
            return redirect('it_dashboard')
        job = get_object_or_404(MaintenanceJob, pk=pk)
        action = request.POST.get('action', '')

        try:
            if action == 'assign':
                technician = get_object_or_404(User, pk=request.POST.get('assigned_to_id'))
                maintenance_services.assign_job(job, technician, actor=request.user)
                messages.success(request, f'Assigned to {technician.full_name}.')

            elif action == 'transition':
                maintenance_services.transition_job(
                    job,
                    request.POST.get('status'),
                    actor=request.user,
                    note=request.POST.get('internal_note', '').strip(),
                    public_note=request.POST.get('public_note', '').strip(),
                )
                messages.success(request, 'Maintenance status updated.')

            elif action == 'add_charge':
                maintenance_services.add_charge_line(
                    job,
                    actor=request.user,
                    data={
                        'charge_type': request.POST.get('charge_type'),
                        'description': request.POST.get('description'),
                        'quantity': request.POST.get('quantity'),
                        'unit_price': request.POST.get('unit_price'),
                        'billable': request.POST.get('billable') == 'on',
                        'covered_by_warranty': request.POST.get('covered_by_warranty') == 'on',
                    },
                )
                messages.success(request, 'Charge line added.')

            elif action == 'delete_charge':
                line = get_object_or_404(MaintenanceChargeLine, pk=request.POST.get('line_id'), job=job)
                line.delete()
                messages.success(request, 'Charge line removed.')

            elif action == 'update_work':
                fields = []
                for field in [
                    'diagnosis', 'work_performed', 'parts_used',
                    'quality_check_notes', 'owner_visible_note', 'internal_note',
                    'condition_received', 'accessories_received',
                ]:
                    if field in request.POST:
                        setattr(job, field, request.POST.get(field, '').strip())
                        fields.append(field)
                if fields:
                    fields.append('updated_at')
                    job.save(update_fields=fields)
                    MaintenanceEvent.objects.create(
                        job=job,
                        kind=MaintenanceEvent.Kind.NOTE,
                        actor=request.user,
                        message=request.POST.get('owner_visible_note') or 'Maintenance work record updated.',
                        is_public=bool(request.POST.get('owner_visible_note')),
                    )
                messages.success(request, 'Work record updated.')

            elif action == 'update_deadlines':
                maintenance_services.update_deadlines(
                    job,
                    actor=request.user,
                    diagnosis_due_at=_parse_local_datetime(request.POST.get('diagnosis_due_at')),
                    promised_completion_at=_parse_local_datetime(
                        request.POST.get('promised_completion_at')
                    ),
                    pickup_deadline_at=_parse_local_datetime(request.POST.get('pickup_deadline_at')),
                )
                messages.success(request, 'Deadlines and reminders updated.')

            elif action == 'record_payment':
                maintenance_services.record_payment(
                    job,
                    actor=request.user,
                    amount=request.POST.get('amount'),
                    method=request.POST.get('payment_method', ''),
                    reference=request.POST.get('payment_reference', ''),
                )
                messages.success(request, 'Payment recorded and receipt generated when fully paid.')

            elif action == 'generate_invoice':
                maintenance_services.create_invoice_for_job(job, actor=request.user, finalize=True)
                messages.success(request, 'Invoice generated through the invoice app.')

            elif action == 'generate_receipt':
                maintenance_services.create_receipt_for_job(job, actor=request.user, finalize=True)
                messages.success(request, 'Receipt generated through the invoice app.')

            elif action == 'regenerate_token':
                maintenance_services.regenerate_tracking_token(
                    job, actor=request.user, request=request
                )
                messages.success(request, 'Tracking link regenerated and emailed to the owner.')

            elif action == 'resend_tracking':
                if job.status == MaintenanceJob.Status.CLOSED:
                    raise ValueError('The tracking link is closed and cannot be resent.')
                link = maintenance_services.public_tracking_url(job, request=request)
                from apps.it_support.maintenance_notifications import send_tracking_link
                send_tracking_link(job, link, actor=request.user)
                messages.success(request, 'Tracking link notification processed.')

            elif action == 'add_attachment':
                upload = request.FILES.get('file')
                if not upload:
                    raise ValueError('Choose a file to upload.')
                attachment = MaintenanceAttachment.objects.create(
                    job=job,
                    category=request.POST.get('category') or MaintenanceAttachment.Category.OTHER,
                    file=upload,
                    caption=request.POST.get('caption', '').strip(),
                    is_public=request.POST.get('is_public') == 'on',
                    uploaded_by=request.user,
                )
                MaintenanceEvent.objects.create(
                    job=job,
                    kind=MaintenanceEvent.Kind.ATTACHMENT,
                    actor=request.user,
                    message=f'Attachment added: {attachment.file.name}.',
                    is_public=attachment.is_public,
                )
                messages.success(request, 'Attachment uploaded.')

            else:
                messages.error(request, 'Unknown maintenance action.')

        except Exception as exc:
            messages.error(request, str(exc))

        return redirect('maintenance_detail', pk=job.pk)


class MaintenancePublicView(View):
    template_name = 'it_support/maintenance_public.html'
    expired_template_name = 'it_support/maintenance_link_expired.html'

    def _resolve(self, token):
        token_obj = MaintenanceAccessToken.objects.select_related(
            'job', 'job__assigned_to', 'job__invoice', 'job__receipt'
        ).filter(token=token).first()
        if not token_obj or not token_obj.is_valid():
            return None
        token_obj.last_used_at = timezone.now()
        token_obj.view_count += 1
        token_obj.save(update_fields=['last_used_at', 'view_count'])
        return token_obj

    def get(self, request, token):
        token_obj = self._resolve(token)
        if not token_obj:
            return render(request, self.expired_template_name, status=410)
        job = token_obj.job
        return render(request, self.template_name, {
            'job': job,
            'events': job.events.filter(is_public=True).order_by('created_at'),
            'attachments': job.attachments.filter(is_public=True),
            'charge_lines': job.charge_lines.filter(billable=True),
        })

    def post(self, request, token):
        token_obj = self._resolve(token)
        if not token_obj:
            return render(request, self.expired_template_name, status=410)
        job = token_obj.job
        action = request.POST.get('action')
        try:
            if action == 'approve_quote':
                maintenance_services.customer_quote_response(
                    job, approved=True, note=request.POST.get('note', '').strip()
                )
                messages.success(request, 'Thank you. Your approval has been recorded.')
            elif action == 'decline_quote':
                maintenance_services.customer_quote_response(
                    job, approved=False, note=request.POST.get('note', '').strip()
                )
                messages.info(request, 'Your decision has been recorded.')
            else:
                messages.error(request, 'This action is not available.')
        except Exception as exc:
            messages.error(request, str(exc))
        return redirect('maintenance_public', token=token)


class MaintenancePrintView(LoginRequiredMixin, TemplateView):
    template_name = 'it_support/maintenance_print.html'

    def dispatch(self, request, *args, **kwargs):
        if not _is_it_staff(request.user):
            messages.error(request, 'IT staff only.')
            return redirect('it_dashboard')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        job = get_object_or_404(
            MaintenanceJob.objects.select_related(
                'equipment', 'owner_user', 'assigned_to', 'invoice', 'receipt'
            ).prefetch_related('charge_lines'),
            pk=kwargs['pk'],
        )
        token, _ = MaintenanceAccessToken.objects.get_or_create(job=job)
        ctx.update({
            'job': job,
            'tracking_url': self.request.build_absolute_uri(
                reverse('maintenance_public', kwargs={'token': token.token})
            ),
        })
        return ctx
