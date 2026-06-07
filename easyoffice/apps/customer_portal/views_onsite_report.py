"""
apps/customer_portal/views_onsite_report.py
============================================

NEW MODULE — staff-auth views for:

  Technician visit logging
  -------------------------
  * VisitReportDetailView   GET  /onsite/visits/<pk>/
        The technician's logging page — shows the activity rows and the
        add-row form. This is the link a technician follows from a ticket
        or a routine-maintenance task.
  * VisitActivityAddView    POST /onsite/visits/<pk>/activities/add/
  * VisitActivityEditView   POST /onsite/visits/<pk>/activities/<act_pk>/edit/
  * VisitActivityDeleteView POST /onsite/visits/<pk>/activities/<act_pk>/delete/
  * VisitSubmitView         POST /onsite/visits/<pk>/submit/
  * VisitReopenView         POST /onsite/visits/<pk>/reopen/

  Visit creation entry points
  ---------------------------
  * VisitStartForContractView POST /onsite/contracts/<contract_pk>/visit/new/
  * VisitStartForTicketView   POST /onsite/tickets/<assignment_pk>/visit/new/

  Monthly report
  --------------
  * MonthlyReportGenerateView POST /onsite/monthly/generate/
  * MonthlyReportDetailView   GET  /onsite/monthly/<pk>/
  * MonthlyReportSignView     POST /onsite/monthly/<pk>/send-for-signature/
  * MonthlyReportSendView     POST /onsite/monthly/<pk>/send/
  * MonthlyReportDownloadView GET  /onsite/monthly/<pk>/download.pdf
"""
from __future__ import annotations

import logging
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import View, TemplateView

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Permissions
# ─────────────────────────────────────────────────────────────────────────────

def _is_admin_user(user):
    return user.is_superuser or user.groups.filter(
        name__in=['CEO', 'Admin', 'Office Manager'],
    ).exists()


def _can_log_visit(user, visit) -> bool:
    """Assigned technician, their supervisor, or an admin may log a visit."""
    if _is_admin_user(user):
        return True
    if visit.technician_id == user.id:
        return True
    tech_profile = getattr(visit.technician, 'staffprofile', None)
    if tech_profile and tech_profile.supervisor_id == user.id:
        return True
    return False


def _can_manage_reports(user) -> bool:
    """Generating/sending monthly reports is a leadership action."""
    return _is_admin_user(user) or user.groups.filter(
        name__in=['Customer Service', 'Sales'],
    ).exists()


# ─────────────────────────────────────────────────────────────────────────────
# Visit logging
# ─────────────────────────────────────────────────────────────────────────────

class VisitReportDetailView(LoginRequiredMixin, TemplateView):
    template_name = 'customer_portal/visit_report_detail.html'

    def get_context_data(self, **kwargs):
        from apps.customer_portal.models_onsite_report import (
            OnSiteVisitReport, VisitActivity,
        )
        ctx = super().get_context_data(**kwargs)
        visit = get_object_or_404(OnSiteVisitReport, pk=kwargs['pk'])
        if not _can_log_visit(self.request.user, visit):
            self.raise_forbidden = True
        ctx['visit'] = visit
        ctx['activities'] = visit.activities.select_related('logged_by').all()
        ctx['activity_statuses'] = VisitActivity.Status.choices
        ctx['can_edit'] = (
            visit.status == OnSiteVisitReport.Status.DRAFT
            and _can_log_visit(self.request.user, visit)
        )
        ctx['resolved_contract'] = visit.resolved_contract
        return ctx

    def dispatch(self, request, *args, **kwargs):
        from apps.customer_portal.models_onsite_report import OnSiteVisitReport
        visit = get_object_or_404(OnSiteVisitReport, pk=kwargs['pk'])
        if not _can_log_visit(request.user, visit):
            return HttpResponseForbidden('You cannot view this visit report.')
        return super().dispatch(request, *args, **kwargs)


class VisitActivityAddView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        from apps.customer_portal.models_onsite_report import (
            OnSiteVisitReport, VisitActivity,
        )
        visit = get_object_or_404(OnSiteVisitReport, pk=pk)
        if not _can_log_visit(request.user, visit):
            return HttpResponseForbidden('You cannot log activities on this visit.')
        if visit.status != OnSiteVisitReport.Status.DRAFT:
            messages.error(request, 'This visit has been submitted and is locked. Reopen it to edit.')
            return redirect('visit_report_detail', pk=visit.pk)

        task_description = (request.POST.get('task_description') or '').strip()
        if not task_description:
            messages.error(request, 'Task / issue description is required.')
            return redirect('visit_report_detail', pk=visit.pk)

        activity_date = _parse_date(request.POST.get('activity_date')) or timezone.localdate()
        status = (request.POST.get('status') or VisitActivity.Status.DONE)
        if status not in dict(VisitActivity.Status.choices):
            status = VisitActivity.Status.DONE

        next_pos = (visit.activities.count())
        VisitActivity.objects.create(
            visit=visit,
            position=next_pos,
            activity_date=activity_date,
            device=(request.POST.get('device') or '').strip()[:200],
            task_description=task_description[:500],
            serial_or_user=(request.POST.get('serial_or_user') or '').strip()[:200],
            status=status,
            notes=(request.POST.get('notes') or '').strip(),
            logged_by=request.user,
        )

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'status': 'ok', 'count': visit.activities.count()})
        messages.success(request, 'Activity added.')
        return redirect('visit_report_detail', pk=visit.pk)


class VisitActivityEditView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request, pk, act_pk):
        from apps.customer_portal.models_onsite_report import (
            OnSiteVisitReport, VisitActivity,
        )
        visit = get_object_or_404(OnSiteVisitReport, pk=pk)
        if not _can_log_visit(request.user, visit):
            return HttpResponseForbidden('You cannot edit this visit.')
        if visit.status != OnSiteVisitReport.Status.DRAFT:
            messages.error(request, 'This visit is locked. Reopen it to edit.')
            return redirect('visit_report_detail', pk=visit.pk)

        activity = get_object_or_404(VisitActivity, pk=act_pk, visit=visit)
        task_description = (request.POST.get('task_description') or '').strip()
        if not task_description:
            messages.error(request, 'Task / issue description is required.')
            return redirect('visit_report_detail', pk=visit.pk)

        status = (request.POST.get('status') or activity.status)
        if status not in dict(VisitActivity.Status.choices):
            status = activity.status

        activity.activity_date = _parse_date(request.POST.get('activity_date')) or activity.activity_date
        activity.device = (request.POST.get('device') or '').strip()[:200]
        activity.task_description = task_description[:500]
        activity.serial_or_user = (request.POST.get('serial_or_user') or '').strip()[:200]
        activity.status = status
        activity.notes = (request.POST.get('notes') or '').strip()
        activity.save()

        messages.success(request, 'Activity updated.')
        return redirect('visit_report_detail', pk=visit.pk)


class VisitActivityDeleteView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request, pk, act_pk):
        from apps.customer_portal.models_onsite_report import (
            OnSiteVisitReport, VisitActivity,
        )
        visit = get_object_or_404(OnSiteVisitReport, pk=pk)
        if not _can_log_visit(request.user, visit):
            return HttpResponseForbidden('You cannot edit this visit.')
        if visit.status != OnSiteVisitReport.Status.DRAFT:
            messages.error(request, 'This visit is locked. Reopen it to edit.')
            return redirect('visit_report_detail', pk=visit.pk)

        activity = get_object_or_404(VisitActivity, pk=act_pk, visit=visit)
        activity.delete()
        messages.success(request, 'Activity removed.')
        return redirect('visit_report_detail', pk=visit.pk)


class VisitSubmitView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        from apps.customer_portal.models_onsite_report import OnSiteVisitReport
        visit = get_object_or_404(OnSiteVisitReport, pk=pk)
        if not _can_log_visit(request.user, visit):
            return HttpResponseForbidden('You cannot submit this visit.')
        if visit.activity_count == 0:
            messages.error(request, 'Add at least one activity before submitting.')
            return redirect('visit_report_detail', pk=visit.pk)

        visit.summary = (request.POST.get('summary') or visit.summary or '').strip()
        visit.site_location = (request.POST.get('site_location') or visit.site_location or '').strip()[:255]
        visit.save(update_fields=['summary', 'site_location', 'updated_at'])
        visit.submit(by_user=request.user)
        messages.success(request, 'Visit submitted. It will be included in the monthly report.')
        return redirect('visit_report_detail', pk=visit.pk)


class VisitReopenView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        from apps.customer_portal.models_onsite_report import OnSiteVisitReport
        visit = get_object_or_404(OnSiteVisitReport, pk=pk)
        if not _can_log_visit(request.user, visit):
            return HttpResponseForbidden('You cannot reopen this visit.')
        visit.reopen()
        messages.info(request, 'Visit reopened for editing.')
        return redirect('visit_report_detail', pk=visit.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Visit creation entry points
# ─────────────────────────────────────────────────────────────────────────────

class VisitStartForContractView(LoginRequiredMixin, View):
    """Start a routine-maintenance visit for a contract."""
    http_method_names = ['post']

    def post(self, request, contract_pk):
        from apps.finance.models import Contract
        from apps.customer_portal.models_onsite_report import OnSiteVisitReport
        contract = get_object_or_404(Contract, pk=contract_pk)

        visit = OnSiteVisitReport.objects.create(
            origin=OnSiteVisitReport.Origin.ROUTINE,
            contract=contract,
            technician=request.user,
            visit_date=timezone.localdate(),
            site_location=(request.POST.get('site_location') or '').strip()[:255],
        )
        messages.success(request, 'New routine-maintenance visit started. Log your activities below.')
        return redirect('visit_report_detail', pk=visit.pk)


class VisitStartForTicketView(LoginRequiredMixin, View):
    """Start a ticket-driven visit from a ServiceTicketAssignment."""
    http_method_names = ['post']

    def post(self, request, assignment_pk):
        from apps.customer_service.models import ServiceTicketAssignment
        from apps.customer_portal.models_onsite_report import OnSiteVisitReport
        assignment = get_object_or_404(ServiceTicketAssignment, pk=assignment_pk)

        contract = getattr(getattr(assignment, 'ticket', None), 'contract', None)
        visit = OnSiteVisitReport.objects.create(
            origin=OnSiteVisitReport.Origin.TICKET,
            ticket_assignment=assignment,
            contract=contract,
            task=getattr(assignment, 'task_ref', None),
            technician=request.user,
            visit_date=timezone.localdate(),
        )
        messages.success(request, 'New ticket visit started. Log your activities below.')
        return redirect('visit_report_detail', pk=visit.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Monthly report
# ─────────────────────────────────────────────────────────────────────────────

class MonthlyReportGenerateView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request):
        from apps.finance.models import Contract
        from apps.customer_portal.models_onsite_report import MonthlyServiceReport
        from apps.customer_portal import onsite_report_service as svc

        if not _can_manage_reports(request.user):
            return HttpResponseForbidden('You cannot generate monthly reports.')

        scope = request.POST.get('scope') or MonthlyServiceReport.Scope.CONTRACT
        # Period: a "YYYY-MM" month selector
        ym = (request.POST.get('month') or '').strip()
        try:
            year, month = (int(x) for x in ym.split('-'))
            period_start, period_end = svc.month_bounds(year, month)
        except (ValueError, TypeError):
            messages.error(request, 'Pick a valid month (YYYY-MM).')
            return redirect(request.META.get('HTTP_REFERER', 'finance_dashboard'))

        contract = None
        customer_name = ''
        if scope == MonthlyServiceReport.Scope.CONTRACT:
            contract = get_object_or_404(Contract, pk=request.POST.get('contract'))
        else:
            customer_name = (request.POST.get('customer_name') or '').strip()
            if not customer_name and request.POST.get('contract'):
                c = Contract.objects.filter(pk=request.POST.get('contract')).first()
                customer_name = c.counterparty_name if c else ''
            if not customer_name:
                messages.error(request, 'Provide a customer for a customer-scope report.')
                return redirect(request.META.get('HTTP_REFERER', 'finance_dashboard'))

        try:
            report = svc.generate_monthly_report(
                scope=scope, contract=contract, customer_name=customer_name,
                period_start=period_start, period_end=period_end,
                actor=request.user,
            )
        except svc.OnSiteReportError as e:
            messages.error(request, f'Could not generate report: {e}')
            return redirect(request.META.get('HTTP_REFERER', 'finance_dashboard'))

        messages.success(
            request,
            f'Generated {report.period_label} report '
            f'({report.total_activities} activities, {report.total_visits} visits).',
        )
        return redirect('monthly_report_detail', pk=report.pk)


class MonthlyReportDetailView(LoginRequiredMixin, TemplateView):
    template_name = 'customer_portal/monthly_report_detail.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_reports(request.user):
            return HttpResponseForbidden('You cannot view monthly reports.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        from apps.customer_portal.models_onsite_report import (
            MonthlyServiceReport, OnSiteVisitReport,
        )
        ctx = super().get_context_data(**kwargs)
        report = get_object_or_404(MonthlyServiceReport, pk=kwargs['pk'])
        ctx['report'] = report
        ctx['visits'] = OnSiteVisitReport.objects.filter(
            pk__in=report.visit_ids
        ).select_related('technician').prefetch_related('activities')
        ctx['sig'] = report.signature_request
        return ctx


class MonthlyReportSignView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        from apps.customer_portal.models_onsite_report import MonthlyServiceReport
        from apps.customer_portal import onsite_report_service as svc

        if not _can_manage_reports(request.user):
            return HttpResponseForbidden('You cannot send reports for signature.')

        report = get_object_or_404(MonthlyServiceReport, pk=pk)
        ceo_name = (request.POST.get('ceo_name') or '').strip()
        ceo_email = (request.POST.get('ceo_email') or '').strip()
        if not ceo_name or not ceo_email:
            messages.error(request, 'CEO name and email are required.')
            return redirect('monthly_report_detail', pk=report.pk)

        try:
            sig_req = svc.send_report_for_ceo_signature(
                report, ceo_name=ceo_name, ceo_email=ceo_email, actor=request.user,
            )
        except svc.OnSiteReportError as e:
            messages.error(request, str(e))
            return redirect('monthly_report_detail', pk=report.pk)

        # Hand off to the existing contract signature send flow if present.
        try:
            from apps.finance.contract_document_service import mark_request_sent
            mark_request_sent(sig_req)
        except Exception:
            logger.exception('monthly_report: mark_request_sent failed')

        messages.success(
            request,
            f'Report sent to {ceo_name} for digital signature.',
        )
        return redirect('monthly_report_detail', pk=report.pk)


class MonthlyReportSendView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request, pk):
        from apps.customer_portal.models_onsite_report import MonthlyServiceReport
        from apps.customer_portal import onsite_report_service as svc

        if not _can_manage_reports(request.user):
            return HttpResponseForbidden('You cannot send reports.')

        report = get_object_or_404(MonthlyServiceReport, pk=pk)
        try:
            svc.email_signed_report(report)
        except svc.OnSiteReportError as e:
            messages.error(request, str(e))
            return redirect('monthly_report_detail', pk=report.pk)
        except Exception as e:
            logger.exception('monthly_report: email failed')
            messages.error(request, f'Failed to email report: {e}')
            return redirect('monthly_report_detail', pk=report.pk)

        messages.success(request, 'Signed report emailed to the customer.')
        return redirect('monthly_report_detail', pk=report.pk)


class MonthlyReportDownloadView(LoginRequiredMixin, View):
    http_method_names = ['get']

    def get(self, request, pk):
        from apps.customer_portal.models_onsite_report import MonthlyServiceReport
        if not _can_manage_reports(request.user):
            return HttpResponseForbidden()
        report = get_object_or_404(MonthlyServiceReport, pk=pk)
        # Prefer the signed PDF if the CEO has signed.
        field = None
        if report.signature_request and getattr(report.signature_request, 'signed_pdf', None):
            field = report.signature_request.signed_pdf
        field = field or report.pdf_file
        if not field:
            raise Http404('No PDF available.')
        field.open('rb')
        try:
            data = field.read()
        finally:
            field.close()
        resp = HttpResponse(data, content_type='application/pdf')
        resp['Content-Disposition'] = (
            f'inline; filename="service-report-{report.period_start:%Y-%m}.pdf"'
        )
        return resp


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_date(raw):
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None
