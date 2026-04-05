from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, DetailView, View
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.utils import timezone
from django.db.models import Q, Count
from apps.it_support.models import ITRequest, Equipment, TicketComment
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
                if new_status != 'issued':
                    equipment.current_user = None
                    equipment.issued_date = None
                equipment.save()
                messages.success(request, f'{equipment.name} status updated to {new_status}.')

        next_url = request.POST.get('next', '')
        if next_url and next_url.startswith('/'):
            return redirect(next_url)
        return redirect('equipment_list')