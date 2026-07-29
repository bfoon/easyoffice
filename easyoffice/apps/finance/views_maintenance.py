"""
apps/finance/views_maintenance.py
=================================

Views for the contract maintenance roster.

Internal (login required, same _can_manage_contracts gate as the rest of the
contract module):
    * MaintenanceRosterCreateView   — add a roster to a contract
    * MaintenanceRosterUpdateView   — edit / activate / deactivate
    * MaintenanceRosterDeleteView
    * MaintenanceMemberAddView      — add internal user or external contact
    * MaintenanceMemberRemoveView
    * MaintenanceVisitScheduleNowView — manually schedule the next visit now
    * MaintenanceVisitResendView    — resend calendar invites
    * MaintenanceVisitCancelView

Public (token, no login — same pattern as ContractPublicSignView):
    * MaintenanceVisitCompleteView  — GET shows the completion form,
                                      POST records the report + closes Task
"""
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import View

from apps.core.models import User
from apps.finance.models import (
    Contract, MaintenanceRoster, MaintenanceRosterMember, MaintenanceVisit,
)
from apps.finance.contract_maintenance_service import (
    MaintenanceError,
    cancel_visit,
    complete_visit,
    schedule_visit_for_roster,
    send_visit_invites,
)
from apps.finance.views import _can_manage_contracts, _parse_date


# ── Shared plumbing ─────────────────────────────────────────────────────────

class _ManageContractMixin(LoginRequiredMixin):
    """Load the contract and enforce the manage-contracts permission."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        if not _can_manage_contracts(request.user):
            return HttpResponseForbidden('You do not have permission to manage contracts.')
        self.contract = get_object_or_404(Contract, pk=kwargs['pk'])
        return super().dispatch(request, *args, **kwargs)

    def back(self):
        return redirect('contract_detail', pk=self.contract.pk)


def _get_roster(contract, roster_pk):
    return get_object_or_404(MaintenanceRoster, pk=roster_pk, contract=contract)


# ── Roster CRUD ─────────────────────────────────────────────────────────────

class MaintenanceRosterCreateView(_ManageContractMixin, View):

    def post(self, request, pk):
        title = request.POST.get('title', '').strip()
        if not title:
            messages.error(request, 'A roster title is required.')
            return self.back()

        try:
            start_date = _parse_date(request.POST.get('start_date'), 'first visit date')
            end_date = _parse_date(request.POST.get('end_date'), 'end date')
        except ValueError as e:
            messages.error(request, str(e))
            return self.back()

        if not start_date:
            messages.error(request, 'The first visit date is required.')
            return self.back()

        visit_time = None
        raw_time = request.POST.get('visit_time', '').strip()
        if raw_time:
            try:
                visit_time = datetime.strptime(raw_time, '%H:%M').time()
            except ValueError:
                messages.error(request, 'Visit time must be in HH:MM format.')
                return self.back()

        frequency = request.POST.get('frequency', MaintenanceRoster.Frequency.MONTHLY)
        if frequency not in dict(MaintenanceRoster.Frequency.choices):
            frequency = MaintenanceRoster.Frequency.MONTHLY

        roster = MaintenanceRoster.objects.create(
            contract=self.contract,
            title=title,
            description=request.POST.get('description', '').strip(),
            location=request.POST.get('location', '').strip(),
            frequency=frequency,
            start_date=start_date,
            end_date=end_date,
            visit_time=visit_time,
            duration_minutes=int(request.POST.get('duration_minutes') or 60),
            notice_days_before=int(request.POST.get('notice_days_before') or 7),
            active=request.POST.get('active', 'on') == 'on',
            send_calendar_invites=request.POST.get('send_calendar_invites', 'on') == 'on',
            create_task=request.POST.get('create_task', 'on') == 'on',
            created_by=request.user,
        )
        messages.success(
            request,
            f'Maintenance roster "{roster.title}" created. '
            f'Add responsible parties so invites can be sent.'
        )
        return self.back()


class MaintenanceRosterUpdateView(_ManageContractMixin, View):

    def post(self, request, pk, roster_pk):
        roster = _get_roster(self.contract, roster_pk)

        # Simple toggle shortcut: ?action=toggle flips activation
        if request.POST.get('action') == 'toggle':
            roster.active = not roster.active
            roster.save(update_fields=['active', 'updated_at'])
            messages.success(
                request,
                f'Roster "{roster.title}" {"activated" if roster.active else "deactivated"}.'
            )
            return self.back()

        roster.title = request.POST.get('title', roster.title).strip() or roster.title
        roster.description = request.POST.get('description', roster.description).strip()
        roster.location = request.POST.get('location', roster.location).strip()

        frequency = request.POST.get('frequency')
        if frequency in dict(MaintenanceRoster.Frequency.choices):
            roster.frequency = frequency

        try:
            nvd = _parse_date(request.POST.get('next_visit_date'), 'next visit date')
            end = _parse_date(request.POST.get('end_date'), 'end date')
        except ValueError as e:
            messages.error(request, str(e))
            return self.back()
        if nvd:
            roster.next_visit_date = nvd
        roster.end_date = end

        raw_time = request.POST.get('visit_time', '').strip()
        if raw_time:
            try:
                roster.visit_time = datetime.strptime(raw_time, '%H:%M').time()
            except ValueError:
                pass

        roster.duration_minutes = int(request.POST.get('duration_minutes') or roster.duration_minutes)
        roster.notice_days_before = int(request.POST.get('notice_days_before') or roster.notice_days_before)
        roster.send_calendar_invites = request.POST.get('send_calendar_invites') == 'on'
        roster.create_task = request.POST.get('create_task') == 'on'
        roster.active = request.POST.get('active') == 'on'
        roster.save()

        messages.success(request, f'Roster "{roster.title}" updated.')
        return self.back()


class MaintenanceRosterDeleteView(_ManageContractMixin, View):

    def post(self, request, pk, roster_pk):
        roster = _get_roster(self.contract, roster_pk)
        title = roster.title
        roster.delete()
        messages.success(request, f'Roster "{title}" deleted.')
        return self.back()


# ── Members (responsible parties) ───────────────────────────────────────────

class MaintenanceMemberAddView(_ManageContractMixin, View):

    def post(self, request, pk, roster_pk):
        roster = _get_roster(self.contract, roster_pk)

        user_id = request.POST.get('user') or None
        external_name = request.POST.get('external_name', '').strip()
        external_email = request.POST.get('external_email', '').strip()
        is_lead = request.POST.get('is_lead') == 'on'

        if user_id:
            user = User.objects.filter(pk=user_id, is_active=True).first()
            if not user:
                messages.error(request, 'Selected staff member not found.')
                return self.back()
            if roster.members.filter(user=user).exists():
                messages.info(request, f'{user.get_full_name() or user.username} is already on this roster.')
                return self.back()
            member = MaintenanceRosterMember(roster=roster, user=user, is_lead=is_lead)
        elif external_email:
            member = MaintenanceRosterMember(
                roster=roster,
                external_name=external_name or external_email,
                external_email=external_email,
                is_lead=is_lead,
            )
        else:
            messages.error(request, 'Pick a staff member or provide an external email.')
            return self.back()

        if is_lead:
            roster.members.update(is_lead=False)
        member.save()
        messages.success(request, f'{member.display_name} added as a responsible party.')
        return self.back()


class MaintenanceMemberRemoveView(_ManageContractMixin, View):

    def post(self, request, pk, roster_pk, member_pk):
        roster = _get_roster(self.contract, roster_pk)
        member = get_object_or_404(MaintenanceRosterMember, pk=member_pk, roster=roster)
        name = member.display_name
        member.delete()
        messages.success(request, f'{name} removed from the roster.')
        return self.back()


# ── Visit actions ───────────────────────────────────────────────────────────

class MaintenanceVisitScheduleNowView(_ManageContractMixin, View):
    """Manually materialize the next visit (task + invites) right now."""

    def post(self, request, pk, roster_pk):
        roster = _get_roster(self.contract, roster_pk)
        try:
            override = _parse_date(request.POST.get('visit_date'), 'visit date')
        except ValueError as e:
            messages.error(request, str(e))
            return self.back()

        try:
            visit, created = schedule_visit_for_roster(
                roster, visit_date=override, actor=request.user, request=request,
            )
        except MaintenanceError as e:
            messages.error(request, str(e))
            return self.back()

        if created:
            messages.success(
                request,
                f'Visit on {visit.scheduled_date} scheduled — task created and '
                f'calendar invites sent to all responsible parties.'
            )
        else:
            messages.info(request, f'A visit on {visit.scheduled_date} already exists.')
        return self.back()


class MaintenanceVisitResendView(_ManageContractMixin, View):

    def post(self, request, pk, roster_pk, visit_pk):
        roster = _get_roster(self.contract, roster_pk)
        visit = get_object_or_404(MaintenanceVisit, pk=visit_pk, roster=roster)
        result = send_visit_invites(visit, request=request)
        if result['sent']:
            messages.success(request, f'Calendar invites resent to {result["sent"]} responsible part{"y" if result["sent"] == 1 else "ies"}.')
        for who, err in result['errors']:
            messages.warning(request, f'Could not invite {who}: {err}')
        return self.back()


class MaintenanceVisitCancelView(_ManageContractMixin, View):

    def post(self, request, pk, roster_pk, visit_pk):
        roster = _get_roster(self.contract, roster_pk)
        visit = get_object_or_404(MaintenanceVisit, pk=visit_pk, roster=roster)
        try:
            cancel_visit(visit, actor=request.user)
        except MaintenanceError as e:
            messages.error(request, str(e))
            return self.back()
        messages.success(request, f'Visit on {visit.scheduled_date} cancelled and calendar event retracted.')
        return self.back()


# ── Public completion form (token, no login) ────────────────────────────────

class MaintenanceVisitCompleteView(View):
    """
    GET  /finance/maintenance/complete/<token>/  -> show completion form
    POST /finance/maintenance/complete/<token>/  -> record report, close task
    """
    template_name = 'finance/maintenance_public_complete.html'

    def _get_visit(self, token):
        return get_object_or_404(
            MaintenanceVisit.objects.select_related('roster', 'roster__contract'),
            access_token=token,
        )

    def get(self, request, token):
        visit = self._get_visit(token)
        return render(request, self.template_name, {
            'visit': visit,
            'roster': visit.roster,
            'contract': visit.roster.contract,
            'outcome_choices': MaintenanceVisit.Outcome.choices,
            'already_done': visit.status == MaintenanceVisit.Status.COMPLETED,
            'cancelled': visit.status == MaintenanceVisit.Status.CANCELLED,
        })

    def post(self, request, token):
        visit = self._get_visit(token)
        name = request.POST.get('name', '').strip()
        outcome = request.POST.get('outcome', '')

        errors = []
        if not name:
            errors.append('Please enter your name.')
        if outcome not in dict(MaintenanceVisit.Outcome.choices):
            errors.append('Please choose an outcome.')

        if errors:
            for e in errors:
                messages.error(request, e)
            return redirect('maintenance_visit_complete', token=token)

        try:
            complete_visit(
                visit,
                name=name,
                outcome=outcome,
                work_done=request.POST.get('work_done', ''),
                issues_found=request.POST.get('issues_found', ''),
                follow_up_required=request.POST.get('follow_up_required') == 'on',
                follow_up_notes=request.POST.get('follow_up_notes', ''),
                user=request.user if request.user.is_authenticated else None,
            )
        except MaintenanceError as e:
            messages.error(request, str(e))
            return redirect('maintenance_visit_complete', token=token)

        messages.success(request, 'Thank you — the completion report has been recorded.')
        return redirect('maintenance_visit_complete', token=token)
