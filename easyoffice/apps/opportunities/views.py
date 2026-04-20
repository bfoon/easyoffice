import hashlib

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import (
    TemplateView,
    View,
    ListView,
    CreateView,
    UpdateView,
    DeleteView,
    DetailView,
)

from apps.opportunities.models import OpportunitySource, OpportunityMatch, OpportunityKeyword
from apps.tasks.models import Task
from apps.opportunities.services import scan_source, scan_all_sources


def _can_manage_opportunities(user):
    return user.is_superuser or user.groups.filter(
        name__in=['CEO', 'Admin', 'Office Manager']
    ).exists()


class OpportunityDashboardView(LoginRequiredMixin, TemplateView):
    template_name = 'opportunities/dashboard.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_opportunities(request.user):
            return HttpResponseForbidden('You do not have permission to access opportunity monitoring.')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        keyword_q = (self.request.GET.get('keyword') or '').strip()
        country_q = (self.request.GET.get('country') or '').strip()
        source_q = (self.request.GET.get('source') or '').strip()

        qs = OpportunityMatch.objects.select_related('source', 'keyword').all()

        if keyword_q:
            qs = qs.filter(
                Q(keyword__keyword__icontains=keyword_q) |
                Q(title__icontains=keyword_q) |
                Q(summary__icontains=keyword_q) |
                Q(procurement_process__icontains=keyword_q)
            )

        if country_q:
            qs = qs.filter(country__icontains=country_q)

        if source_q:
            qs = qs.filter(source_id=source_q)

        ctx['sources'] = OpportunitySource.objects.order_by('name')
        ctx['recent_matches'] = qs.order_by('-detected_at')[:30]
        ctx['total_sources'] = OpportunitySource.objects.filter(is_active=True).count()
        ctx['inactive_sources'] = OpportunitySource.objects.filter(is_active=False).count()
        ctx['unread_matches'] = OpportunityMatch.objects.filter(is_read=False).count()
        ctx['total_matches'] = OpportunityMatch.objects.count()
        ctx['keywords_count'] = OpportunityKeyword.objects.filter(is_active=True).count()
        ctx['task_created_count'] = OpportunityMatch.objects.filter(is_task_created=True).count()

        ctx['filter_keyword'] = keyword_q
        ctx['filter_country'] = country_q
        ctx['filter_source'] = source_q
        ctx['available_countries'] = OpportunityMatch.objects.exclude(
            country=''
        ).values_list('country', flat=True).distinct().order_by('country')

        return ctx


class OpportunitySourceListView(LoginRequiredMixin, ListView):
    model = OpportunitySource
    template_name = 'opportunities/source_list.html'
    context_object_name = 'sources'

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_opportunities(request.user):
            return HttpResponseForbidden('You do not have permission to manage opportunity sources.')
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        return OpportunitySource.objects.order_by('name')


class OpportunitySourceCreateView(LoginRequiredMixin, CreateView):
    model = OpportunitySource
    template_name = 'opportunities/source_form.html'
    fields = ['name', 'url', 'source_type', 'is_active', 'scan_interval_minutes']
    success_url = reverse_lazy('opportunity_source_list')

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_opportunities(request.user):
            return HttpResponseForbidden('You do not have permission to create opportunity sources.')
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        for field in form.fields.values():
            if field.widget.__class__.__name__ == 'CheckboxInput':
                field.widget.attrs.update({'class': 'form-check-input'})
            else:
                field.widget.attrs.update({'class': 'form-control'})
        return form

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        messages.success(self.request, 'Opportunity source created successfully.')
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, 'Could not create source. Please correct the form errors below.')
        return super().form_invalid(form)


class OpportunitySourceUpdateView(LoginRequiredMixin, UpdateView):
    model = OpportunitySource
    template_name = 'opportunities/source_form.html'
    fields = ['name', 'url', 'source_type', 'is_active', 'scan_interval_minutes']
    success_url = reverse_lazy('opportunity_source_list')

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_opportunities(request.user):
            return HttpResponseForbidden('You do not have permission to edit opportunity sources.')
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        for field in form.fields.values():
            if field.widget.__class__.__name__ == 'CheckboxInput':
                field.widget.attrs.update({'class': 'form-check-input'})
            else:
                field.widget.attrs.update({'class': 'form-control'})
        return form

    def form_valid(self, form):
        messages.success(self.request, 'Opportunity source updated successfully.')
        return super().form_valid(form)

    def form_invalid(self, form):
        messages.error(self.request, 'Could not update source. Please correct the form errors below.')
        return super().form_invalid(form)


class OpportunitySourceDeleteView(LoginRequiredMixin, DeleteView):
    model = OpportunitySource
    success_url = reverse_lazy('opportunity_source_list')
    template_name = 'opportunities/source_confirm_delete.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_opportunities(request.user):
            return HttpResponseForbidden('You do not have permission to delete opportunity sources.')
        return super().dispatch(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        messages.success(self.request, 'Opportunity source deleted successfully.')
        return super().delete(request, *args, **kwargs)


class OpportunityMatchCreateView(LoginRequiredMixin, CreateView):
    model = OpportunityMatch
    template_name = 'opportunities/match_form.html'
    fields = [
        'source', 'keyword', 'title', 'summary', 'external_url',
        'reference_no', 'country', 'office', 'procurement_process',
        'published_at', 'posted_date', 'deadline'
    ]
    success_url = reverse_lazy('opportunity_list')

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_opportunities(request.user):
            return HttpResponseForbidden('You do not have permission to create opportunity matches.')
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        for field in form.fields.values():
            field.widget.attrs.update({'class': 'form-control'})

        for dt_field in ['published_at', 'posted_date', 'deadline']:
            if dt_field in form.fields:
                form.fields[dt_field].widget.input_type = 'datetime-local'

        if 'summary' in form.fields:
            form.fields['summary'].widget.attrs.update({'rows': 5})

        return form

    def form_valid(self, form):
        obj = form.save(commit=False)

        normalized_ref = (obj.reference_no or '').replace(' ', '').upper().strip()

        if normalized_ref and OpportunityMatch.objects.filter(
            source=obj.source,
            reference_no__iexact=normalized_ref
        ).exists():
            messages.warning(self.request, 'A match with this reference number already exists for this source.')
            return redirect('opportunity_list')

        raw = f"{obj.source_id}|{normalized_ref}|{obj.title.strip().lower()}|{obj.external_url.strip().lower()}"
        obj.reference_no = normalized_ref
        obj.fingerprint = hashlib.sha256(raw.encode('utf-8')).hexdigest()

        if OpportunityMatch.objects.filter(fingerprint=obj.fingerprint).exists():
            messages.warning(self.request, 'This opportunity match already exists.')
            return redirect('opportunity_list')

        obj.save()
        messages.success(self.request, 'Opportunity match added successfully.')
        return redirect(self.success_url)

    def form_invalid(self, form):
        messages.error(self.request, 'Could not save match. Please correct the form errors below.')
        return super().form_invalid(form)


class OpportunityMatchDetailView(LoginRequiredMixin, DetailView):
    model = OpportunityMatch
    template_name = 'opportunities/match_detail.html'
    context_object_name = 'match'

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_opportunities(request.user):
            return HttpResponseForbidden('You do not have permission to view this match.')
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if not obj.is_read:
            obj.is_read = True
            obj.save(update_fields=['is_read'])
        return obj


class OpportunityMatchUpdateView(LoginRequiredMixin, UpdateView):
    model = OpportunityMatch
    template_name = 'opportunities/match_form.html'
    fields = [
        'source', 'keyword', 'title', 'summary', 'external_url',
        'reference_no', 'country', 'office', 'procurement_process',
        'published_at', 'posted_date', 'deadline', 'is_read'
    ]
    success_url = reverse_lazy('opportunity_list')

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_opportunities(request.user):
            return HttpResponseForbidden('You do not have permission to edit this match.')
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        for field in form.fields.values():
            if field.widget.__class__.__name__ == 'CheckboxInput':
                field.widget.attrs.update({'class': 'form-check-input'})
            else:
                field.widget.attrs.update({'class': 'form-control'})

        for dt_field in ['published_at', 'posted_date', 'deadline']:
            if dt_field in form.fields:
                form.fields[dt_field].widget.input_type = 'datetime-local'

        if 'summary' in form.fields:
            form.fields['summary'].widget.attrs.update({'rows': 5})

        return form

    def form_valid(self, form):
        obj = form.save(commit=False)
        normalized_ref = (obj.reference_no or '').replace(' ', '').upper().strip()

        if normalized_ref and OpportunityMatch.objects.filter(
            source=obj.source,
            reference_no__iexact=normalized_ref
        ).exclude(pk=obj.pk).exists():
            messages.warning(self.request, 'Another match with this reference number already exists for this source.')
            return redirect('opportunity_match_detail', pk=obj.pk)

        raw = f"{obj.source_id}|{normalized_ref}|{obj.title.strip().lower()}|{obj.external_url.strip().lower()}"
        obj.reference_no = normalized_ref
        obj.fingerprint = hashlib.sha256(raw.encode('utf-8')).hexdigest()

        duplicate = OpportunityMatch.objects.filter(fingerprint=obj.fingerprint).exclude(pk=obj.pk).exists()
        if duplicate:
            messages.warning(self.request, 'Another match with the same source, reference, title, and link already exists.')
            return redirect('opportunity_match_detail', pk=obj.pk)

        obj.save()
        messages.success(self.request, 'Opportunity match updated successfully.')
        return redirect('opportunity_match_detail', pk=obj.pk)

    def form_invalid(self, form):
        messages.error(self.request, 'Could not update match. Please correct the form errors below.')
        return super().form_invalid(form)


class OpportunityMatchDeleteView(LoginRequiredMixin, DeleteView):
    model = OpportunityMatch
    success_url = reverse_lazy('opportunity_list')
    template_name = 'opportunities/match_confirm_delete.html'

    def dispatch(self, request, *args, **kwargs):
        if not _can_manage_opportunities(request.user):
            return HttpResponseForbidden('You do not have permission to delete this match.')
        return super().dispatch(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        messages.success(self.request, 'Opportunity match deleted successfully.')
        return super().delete(request, *args, **kwargs)


class OpportunityMatchMarkReadView(LoginRequiredMixin, View):
    def post(self, request, pk):
        if not _can_manage_opportunities(request.user):
            return HttpResponseForbidden('You do not have permission.')

        match = get_object_or_404(OpportunityMatch, pk=pk)
        match.is_read = True
        match.save(update_fields=['is_read'])
        messages.success(request, 'Opportunity marked as read.')
        return redirect('opportunity_list')


class OpportunitySourceScanNowView(LoginRequiredMixin, View):
    def post(self, request, pk):
        if not _can_manage_opportunities(request.user):
            return HttpResponseForbidden('You do not have permission.')

        source = get_object_or_404(OpportunitySource, pk=pk)
        created = scan_source(source)
        messages.success(request, f'Scan completed for {source.name}. New matches found: {created}.')
        return redirect('opportunity_source_list')


class OpportunityScanAllView(LoginRequiredMixin, View):
    def post(self, request):
        if not _can_manage_opportunities(request.user):
            return HttpResponseForbidden('You do not have permission.')

        created = scan_all_sources()
        messages.success(request, f'All active sources scanned successfully. New matches found: {created}.')
        return redirect('opportunity_list')


class CreateTaskFromOpportunityView(LoginRequiredMixin, View):
    def post(self, request, pk):
        if not _can_manage_opportunities(request.user):
            return HttpResponseForbidden('You do not have permission to create tasks from opportunities.')

        match = get_object_or_404(OpportunityMatch, pk=pk)

        warning_line = ""
        if match.is_deadline_passed:
            warning_line = "\nWARNING: The deadline has already passed."
        elif match.deadline_state == 'critical':
            warning_line = f"\nWARNING: Deadline is very close ({match.deadline:%d-%b-%Y %H:%M})."
        elif match.is_deadline_soon:
            warning_line = f"\nWARNING: Deadline is approaching soon ({match.deadline:%d-%b-%Y %H:%M})."

        task = Task.objects.create(
            title=f"Review opportunity: {match.title[:220]}",
            description=(
                f"Reference: {match.reference_no or 'N/A'}\n"
                f"Country: {match.country or 'N/A'}\n"
                f"Office: {match.office or 'N/A'}\n"
                f"Procurement Process: {match.procurement_process or 'N/A'}\n"
                f"Posted: {match.posted_date.strftime('%d-%b-%Y %H:%M') if match.posted_date else 'N/A'}\n"
                f"Deadline: {match.deadline.strftime('%d-%b-%Y %H:%M') if match.deadline else 'N/A'}\n"
                f"Source: {match.source.name}\n"
                f"Link: {match.external_url}\n"
                f"{warning_line}\n\n"
                f"Summary:\n{match.summary or ''}"
            ),
            assigned_to=request.user,
            assigned_by=request.user,
            priority='critical' if match.deadline_state in ['passed', 'critical'] else 'high',
            status='todo',
            due_date=match.deadline,
            tags=(
                f"opportunity,{match.source.name},"
                f"{match.keyword.keyword if match.keyword else ''},"
                f"{match.country},{match.reference_no}"
            ),
        )

        match.is_task_created = True
        match.is_read = True
        match.save(update_fields=['is_task_created', 'is_read'])

        messages.success(request, 'Task created from opportunity successfully.')
        return redirect('task_detail', pk=task.pk)