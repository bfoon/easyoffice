"""
Invoice views.

URL surface:
    /invoices/                              → dashboard
    /invoices/new/                          → picker (doc type + letterhead)
    /invoices/<uuid>/edit/                  → builder
    /invoices/<uuid>/                       → detail (read-only)
    /invoices/<uuid>/delete/                → delete draft
    /invoices/<uuid>/void/                  → void finalized
    /invoices/<uuid>/duplicate/             → copy into a new draft

    AJAX:
    /invoices/<uuid>/metadata/              POST → save client/dates/totals
    /invoices/<uuid>/items/                 GET/POST → list/replace line items
    /invoices/<uuid>/layout/                POST → save custom drag positions
    /invoices/<uuid>/preview-pdf/           GET  → stream preview PDF
    /invoices/<uuid>/finalize/              POST → generate PDF + save to files app
    /invoices/letterheads/                  GET  → JSON list of PDFs user can use
"""
import json
import uuid
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q, Count, Sum
from django.http import (
    HttpResponse, JsonResponse, HttpResponseBadRequest, HttpResponseForbidden,
    HttpResponseNotAllowed, Http404,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView

from apps.files.models import SharedFile

from .permissions import InvoiceAccessMixin, can_use_invoices
from .models import (
    InvoiceDocument, InvoiceLineItem, InvoiceCounter, InvoiceTemplate,
    DocType, DOC_TYPE_PREFIX,
)
from .forms import InvoiceMetadataForm, TemplateForm
from .services import (
    finalize_invoice, build_preview_pdf, void_invoice,
    save_invoice_as_template, create_invoice_from_template,
    apply_template_update_to_invoices, convert_invoice,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _user_accessible_pdfs(user):
    """Return a queryset of PDF SharedFile rows the user can see."""
    qs = SharedFile.objects.filter(is_latest=True, name__iendswith='.pdf')
    if user.is_superuser:
        return qs

    own = Q(uploaded_by=user)
    office = Q(visibility=SharedFile.Visibility.OFFICE)
    shared_with = Q(visibility=SharedFile.Visibility.SHARED_WITH, shared_with=user)
    share_access = Q(share_access__user=user)

    # Unit / department visibility
    unit_q = Q()
    dept_q = Q()
    if hasattr(user, 'unit') and user.unit_id:
        unit_q = Q(visibility=SharedFile.Visibility.UNIT, unit=user.unit)
    if hasattr(user, 'department') and user.department_id:
        dept_q = Q(visibility=SharedFile.Visibility.DEPARTMENT, department=user.department)

    return qs.filter(own | office | shared_with | share_access | unit_q | dept_q).distinct()


def _decimal(value, default='0'):
    try:
        return Decimal(str(value or default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


# ── Dashboard ────────────────────────────────────────────────────────────────

class InvoiceDashboardView(InvoiceAccessMixin, TemplateView):
    template_name = 'invoices/dashboard.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = InvoiceDocument.objects.all()

        # Filters
        doc_filter = self.request.GET.get('doc_type', '')
        status_filter = self.request.GET.get('status', '')
        search = self.request.GET.get('q', '').strip()

        if doc_filter in DocType.values:
            qs = qs.filter(doc_type=doc_filter)
        if status_filter in InvoiceDocument.Status.values:
            qs = qs.filter(status=status_filter)
        if search:
            qs = qs.filter(
                Q(number__icontains=search) |
                Q(client_name__icontains=search) |
                Q(po_reference__icontains=search)
            )

        current_year = timezone.now().year
        by_type = {}
        for dt, label in DocType.choices:
            by_type[dt] = {
                'label': label,
                'count': InvoiceDocument.objects.filter(
                    doc_type=dt, year=current_year, status=InvoiceDocument.Status.FINALIZED
                ).count(),
                'prefix': DOC_TYPE_PREFIX[dt],
            }

        finalized_this_year = InvoiceDocument.objects.filter(
            status=InvoiceDocument.Status.FINALIZED, year=current_year
        )
        agg = finalized_this_year.aggregate(total=Sum('total'), n=Count('id'))

        ctx.update({
            'invoices':         qs.select_related('generated_pdf', 'created_by')
                                  .prefetch_related('generated_pdf__signature_requests')[:200],
            'by_type':          by_type,
            'total_count':      agg['n'] or 0,
            'total_value':      agg['total'] or Decimal('0.00'),
            'year':             current_year,
            'draft_count':      InvoiceDocument.objects.filter(status=InvoiceDocument.Status.DRAFT).count(),
            'doc_filter':       doc_filter,
            'status_filter':    status_filter,
            'search':           search,
            'doc_type_choices': DocType.choices,
            'status_choices':   InvoiceDocument.Status.choices,
        })
        return ctx


# ── New invoice: letterhead picker ───────────────────────────────────────────

class NewInvoiceView(InvoiceAccessMixin, TemplateView):
    template_name = 'invoices/new.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        pdfs = _user_accessible_pdfs(self.request.user).order_by('-created_at')[:100]
        ctx['pdfs'] = pdfs
        ctx['doc_type_choices'] = DocType.choices

        # Templates available to this user: own + shared
        user = self.request.user
        ctx['my_templates'] = InvoiceTemplate.objects.filter(
            owner=user
        ).select_related('letterhead').order_by('-updated_at')[:24]
        ctx['shared_templates'] = InvoiceTemplate.objects.filter(
            visibility=InvoiceTemplate.Visibility.SHARED,
        ).exclude(owner=user).select_related('letterhead', 'owner').order_by('-updated_at')[:24]

        return ctx

    def post(self, request, *args, **kwargs):
        doc_type = request.POST.get('doc_type', DocType.INVOICE)
        letterhead_id = request.POST.get('letterhead_id')
        if doc_type not in DocType.values or not letterhead_id:
            messages.error(request, 'Please choose a document type and letterhead.')
            return redirect('invoices:invoice_new')
        try:
            letterhead = _user_accessible_pdfs(request.user).get(pk=letterhead_id)
        except SharedFile.DoesNotExist:
            messages.error(request, 'Selected letterhead is not available.')
            return redirect('invoices:invoice_new')

        invoice = InvoiceDocument.objects.create(
            doc_type=doc_type,
            letterhead=letterhead,
            created_by=request.user,
            payment_terms='Net 30',
        )
        # Seed with one empty line
        InvoiceLineItem.objects.create(
            invoice=invoice, position=0,
            description='', quantity=Decimal('1.00'), unit_price=Decimal('0.00'),
        )
        return redirect('invoices:invoice_edit', pk=invoice.pk)


# ── Builder ──────────────────────────────────────────────────────────────────

class InvoiceBuilderView(InvoiceAccessMixin, TemplateView):
    template_name = 'invoices/builder.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        invoice = get_object_or_404(InvoiceDocument, pk=kwargs['pk'])
        if invoice.is_finalized:
            # Redirect to detail — can't edit finalized
            from django.shortcuts import redirect as _r
            ctx['_redirect'] = reverse('invoices:invoice_detail', kwargs={'pk': invoice.pk})
        ctx['invoice'] = invoice
        ctx['form'] = InvoiceMetadataForm(instance=invoice)
        ctx['items'] = invoice.items.all().order_by('position', 'id')
        ctx['doc_type_choices'] = DocType.choices
        ctx['layout_json'] = json.dumps(invoice.layout_json or {})
        return ctx

    def get(self, request, *args, **kwargs):
        ctx = self.get_context_data(**kwargs)
        if ctx.get('_redirect'):
            return redirect(ctx['_redirect'])
        return self.render_to_response(ctx)


# ── Detail (read-only) ───────────────────────────────────────────────────────

class InvoiceDetailView(InvoiceAccessMixin, TemplateView):
    template_name = 'invoices/detail.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        invoice = get_object_or_404(InvoiceDocument, pk=kwargs['pk'])
        ctx['invoice'] = invoice
        ctx['items']   = invoice.items.all().order_by('position', 'id')
        return ctx


# ── AJAX: save metadata ──────────────────────────────────────────────────────

@method_decorator(require_POST, name='dispatch')
class InvoiceMetadataSaveView(InvoiceAccessMixin, View):
    def post(self, request, pk):
        invoice = get_object_or_404(InvoiceDocument, pk=pk)
        if invoice.is_finalized:
            return JsonResponse({'ok': False, 'error': 'Invoice is finalized.'}, status=400)
        if invoice.is_locked_by_conversion:
            return JsonResponse({'ok': False,
                'error': 'This document has been converted and is read-only.'}, status=403)

        # If invoice is locked to a specific doc type by its template, force it
        # back to the template's value regardless of what the client submitted.
        post_data = request.POST.copy()
        if invoice.is_doc_type_locked:
            post_data['doc_type'] = invoice.template.doc_type

        form = InvoiceMetadataForm(post_data, instance=invoice)
        if not form.is_valid():
            return JsonResponse({'ok': False, 'errors': form.errors}, status=400)
        form.save()
        invoice.recalculate_totals(save=True)
        return JsonResponse({
            'ok': True,
            'preview_number': invoice.preview_number,
            'title_text':     invoice.title_text,
            'subtotal':       str(invoice.subtotal),
            'tax_amount':     str(invoice.tax_amount),
            'total':          str(invoice.total),
        })


# ── AJAX: items ──────────────────────────────────────────────────────────────

@method_decorator(require_POST, name='dispatch')
class InvoiceItemsSaveView(InvoiceAccessMixin, View):
    """
    Full replacement of the line items list. Body: {"items":[{description,quantity,unit_price}, ...]}
    """
    def post(self, request, pk):
        invoice = get_object_or_404(InvoiceDocument, pk=pk)
        if invoice.is_finalized:
            return JsonResponse({'ok': False, 'error': 'Invoice is finalized.'}, status=400)
        if invoice.is_locked_by_conversion:
            return JsonResponse({'ok': False,
                'error': 'This document has been converted and is read-only.'}, status=403)
        try:
            payload = json.loads(request.body.decode('utf-8'))
        except Exception:
            return HttpResponseBadRequest('Invalid JSON')

        items = payload.get('items', [])
        if not isinstance(items, list):
            return HttpResponseBadRequest('items must be a list')

        invoice.items.all().delete()
        new_items = []
        for idx, raw in enumerate(items):
            desc = str(raw.get('description', '')).strip()
            qty  = _decimal(raw.get('quantity'),   '0')
            pr   = _decimal(raw.get('unit_price'), '0')
            if not desc and qty == 0 and pr == 0:
                continue
            new_items.append(InvoiceLineItem(
                invoice=invoice,
                position=idx,
                description=desc[:500],
                quantity=qty,
                unit_price=pr,
            ))
        # Bulk create individually so save() computes line_total
        for li in new_items:
            li.save()

        invoice.recalculate_totals(save=True)

        return JsonResponse({
            'ok': True,
            'subtotal':   str(invoice.subtotal),
            'tax_amount': str(invoice.tax_amount),
            'total':      str(invoice.total),
            'items': [
                {
                    'id': str(li.id),
                    'description': li.description,
                    'quantity': str(li.quantity),
                    'unit_price': str(li.unit_price),
                    'line_total': str(li.line_total),
                }
                for li in invoice.items.all().order_by('position', 'id')
            ],
        })


# ── AJAX: save layout (drag positions) ───────────────────────────────────────

@method_decorator(require_POST, name='dispatch')
class InvoiceLayoutSaveView(InvoiceAccessMixin, View):
    def post(self, request, pk):
        invoice = get_object_or_404(InvoiceDocument, pk=pk)
        if invoice.is_finalized:
            return JsonResponse({'ok': False, 'error': 'Invoice is finalized.'}, status=400)
        if invoice.is_locked_by_conversion:
            return JsonResponse({'ok': False,
                'error': 'This document has been converted and is read-only.'}, status=403)
        if invoice.is_layout_locked:
            return JsonResponse({
                'ok': False,
                'error': 'This invoice uses a locked template. Layout cannot be modified.',
            }, status=403)
        try:
            data = json.loads(request.body.decode('utf-8'))
        except Exception:
            return HttpResponseBadRequest('Invalid JSON')
        if not isinstance(data, dict):
            return HttpResponseBadRequest('Layout must be an object')
        # Sanitize: only known keys, only numeric x/y/w percents
        allowed = {'title', 'number_block', 'bill_to', 'ship_to',
                   'items_table', 'totals', 'bank_details', 'notes'}
        clean = {}
        for k, v in data.items():
            if k not in allowed or not isinstance(v, dict):
                continue
            entry = {}
            for coord in ('x_pct', 'y_pct', 'w_pct'):
                if coord in v:
                    try:
                        entry[coord] = max(0.0, min(100.0, float(v[coord])))
                    except (TypeError, ValueError):
                        pass
            if entry:
                clean[k] = entry
        invoice.layout_json = clean
        invoice.save(update_fields=['layout_json', 'updated_at'])
        return JsonResponse({'ok': True})


# ── AJAX: preview PDF ────────────────────────────────────────────────────────

class InvoicePreviewPDFView(InvoiceAccessMixin, View):
    def get(self, request, pk):
        invoice = get_object_or_404(InvoiceDocument, pk=pk)
        if invoice.is_finalized and invoice.generated_pdf:
            # stream the actual saved file
            invoice.generated_pdf.file.open('rb')
            try:
                data = invoice.generated_pdf.file.read()
            finally:
                invoice.generated_pdf.file.close()
            resp = HttpResponse(data, content_type='application/pdf')
            resp['Content-Disposition'] = f'inline; filename="{invoice.number}.pdf"'
            return resp

        pdf = build_preview_pdf(invoice)
        resp = HttpResponse(pdf, content_type='application/pdf')
        resp['Content-Disposition'] = f'inline; filename="preview-{invoice.pk}.pdf"'
        return resp


# ── AJAX: finalize ───────────────────────────────────────────────────────────

@method_decorator(require_POST, name='dispatch')
class InvoiceFinalizeView(InvoiceAccessMixin, View):
    def post(self, request, pk):
        invoice = get_object_or_404(InvoiceDocument, pk=pk)
        if invoice.is_finalized:
            return JsonResponse({'ok': True, 'redirect': reverse('invoices:invoice_detail', kwargs={'pk': invoice.pk})})
        try:
            invoice = finalize_invoice(invoice, request.user)
        except ValueError as e:
            return JsonResponse({'ok': False, 'error': str(e)}, status=400)
        return JsonResponse({
            'ok': True,
            'number': invoice.number,
            'redirect': reverse('invoices:invoice_detail', kwargs={'pk': invoice.pk}),
        })


# ── AJAX: letterheads list (for the picker) ──────────────────────────────────

class LetterheadListView(InvoiceAccessMixin, View):
    def get(self, request):
        q = request.GET.get('q', '').strip()
        qs = _user_accessible_pdfs(request.user)
        if q:
            qs = qs.filter(name__icontains=q)
        qs = qs.order_by('-created_at')[:60]
        return JsonResponse({
            'pdfs': [
                {
                    'id': str(f.id),
                    'name': f.name,
                    'size_display': f.size_display,
                    'uploaded_by': getattr(f.uploaded_by, 'full_name', str(f.uploaded_by)),
                    'created_at': f.created_at.strftime('%Y-%m-%d'),
                    'preview_url': reverse('file_preview', kwargs={'pk': f.pk}),
                }
                for f in qs
            ]
        })


# ── Delete draft ─────────────────────────────────────────────────────────────

@method_decorator(require_POST, name='dispatch')
class InvoiceDeleteView(InvoiceAccessMixin, View):
    def post(self, request, pk):
        invoice = get_object_or_404(InvoiceDocument, pk=pk)
        if invoice.is_finalized:
            messages.error(request, 'Finalized invoices cannot be deleted — use Void instead.')
            return redirect('invoices:invoice_detail', pk=invoice.pk)
        if invoice.is_locked_by_conversion:
            messages.error(request, 'This document has been converted and is read-only.')
            return redirect('invoices:invoice_detail', pk=invoice.pk)
        invoice.delete()
        messages.success(request, 'Draft deleted.')
        return redirect('invoices:invoice_dashboard')


# ── Void finalized ───────────────────────────────────────────────────────────

@method_decorator(require_POST, name='dispatch')
class InvoiceVoidView(InvoiceAccessMixin, View):
    def post(self, request, pk):
        invoice = get_object_or_404(InvoiceDocument, pk=pk)
        if invoice.is_locked_by_conversion:
            messages.error(request, 'This document has been converted to a newer document and cannot be voided.')
            return redirect('invoices:invoice_detail', pk=invoice.pk)
        reason = request.POST.get('reason', '')
        try:
            void_invoice(invoice, request.user, reason)
            messages.success(request, f'{invoice.number} voided.')
        except ValueError as e:
            messages.error(request, str(e))
        return redirect('invoices:invoice_detail', pk=invoice.pk)


# ── Duplicate (finalized → new draft) ────────────────────────────────────────

@method_decorator(require_POST, name='dispatch')
class InvoiceDuplicateView(InvoiceAccessMixin, View):
    def post(self, request, pk):
        src = get_object_or_404(InvoiceDocument, pk=pk)
        dup = InvoiceDocument.objects.create(
            doc_type=src.doc_type,
            letterhead=src.letterhead,
            layout_json=src.layout_json,
            status=InvoiceDocument.Status.DRAFT,
            client_name=src.client_name,
            client_address=src.client_address,
            client_email=src.client_email,
            ship_to_address=src.ship_to_address,
            po_reference='',
            invoice_date=timezone.localdate(),
            due_date=None,
            payment_terms=src.payment_terms,
            currency=src.currency,
            tax_rate=src.tax_rate,
            discount_amount=src.discount_amount,
            bank_details=src.bank_details,
            notes=src.notes,
            created_by=request.user,
        )
        for li in src.items.all().order_by('position', 'id'):
            InvoiceLineItem.objects.create(
                invoice=dup,
                position=li.position,
                description=li.description,
                quantity=li.quantity,
                unit_price=li.unit_price,
            )
        dup.recalculate_totals(save=True)
        messages.success(request, 'Draft created from existing invoice.')
        return redirect('invoices:invoice_edit', pk=dup.pk)


# ── Templates ────────────────────────────────────────────────────────────────

class TemplateListView(InvoiceAccessMixin, TemplateView):
    template_name = 'invoices/template_list.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx['my_templates'] = InvoiceTemplate.objects.filter(
            owner=user
        ).select_related('letterhead').order_by('-updated_at')
        ctx['shared_templates'] = InvoiceTemplate.objects.filter(
            visibility=InvoiceTemplate.Visibility.SHARED,
        ).exclude(owner=user).select_related('letterhead', 'owner').order_by('-updated_at')
        return ctx


@method_decorator(require_POST, name='dispatch')
class TemplateCreateFromInvoiceView(InvoiceAccessMixin, View):
    """
    POST from the builder's 'Save as Template' modal.
    Body fields: name, description, visibility, locked_layout, doc_type
    """
    def post(self, request, pk):
        invoice = get_object_or_404(InvoiceDocument, pk=pk)
        name = (request.POST.get('name') or '').strip()
        if not name:
            return JsonResponse({'ok': False, 'error': 'Template name is required.'}, status=400)

        visibility = request.POST.get('visibility', 'personal')
        if visibility not in ('personal', 'shared'):
            visibility = 'personal'
        locked_layout = request.POST.get('locked_layout') in ('on', 'true', '1', 'yes')
        doc_type = request.POST.get('doc_type', '') or ''

        tmpl = save_invoice_as_template(
            invoice, request.user,
            name=name,
            description=(request.POST.get('description') or '').strip(),
            visibility=visibility,
            locked_layout=locked_layout,
            doc_type=doc_type,
        )
        return JsonResponse({
            'ok': True,
            'template_id': str(tmpl.id),
            'name': tmpl.name,
            'message': f'Template "{tmpl.name}" saved.',
        })


@method_decorator(require_POST, name='dispatch')
class TemplateUseView(InvoiceAccessMixin, View):
    """Create a new draft invoice from a template and redirect to builder."""
    def post(self, request, pk):
        tmpl = get_object_or_404(InvoiceTemplate, pk=pk)
        if not tmpl.user_can_view(request.user):
            messages.error(request, 'You do not have access to that template.')
            return redirect('invoices:invoice_new')
        invoice = create_invoice_from_template(tmpl, request.user)
        return redirect('invoices:invoice_edit', pk=invoice.pk)


class TemplateEditView(InvoiceAccessMixin, View):
    """
    GET renders the edit form; POST saves changes.
    On save, if the 'apply_to_drafts' checkbox is set, pushes the updated
    layout/defaults to any existing DRAFT invoices that use this template.
    """
    template_name = 'invoices/template_form.html'

    def get(self, request, pk):
        tmpl = get_object_or_404(InvoiceTemplate, pk=pk)
        if not tmpl.user_can_edit(request.user):
            messages.error(request, 'You cannot edit this template.')
            return redirect('invoices:template_list')
        form = TemplateForm(instance=tmpl)
        return render(request, self.template_name, {
            'form': form,
            'template': tmpl,
            'is_edit': True,
            'draft_count': InvoiceDocument.objects.filter(
                template=tmpl, status=InvoiceDocument.Status.DRAFT,
            ).count(),
        })

    def post(self, request, pk):
        tmpl = get_object_or_404(InvoiceTemplate, pk=pk)
        if not tmpl.user_can_edit(request.user):
            messages.error(request, 'You cannot edit this template.')
            return redirect('invoices:template_list')
        form = TemplateForm(request.POST, instance=tmpl)
        if not form.is_valid():
            return render(request, self.template_name, {
                'form': form, 'template': tmpl, 'is_edit': True,
            })
        tmpl = form.save()

        # Optional retroactive push
        if request.POST.get('apply_to_drafts') in ('on', 'true', '1'):
            count = apply_template_update_to_invoices(tmpl)
            if count:
                messages.info(request, f'Applied update to {count} draft invoice(s).')

        messages.success(request, f'Template "{tmpl.name}" updated.')
        return redirect('invoices:template_list')


@method_decorator(require_POST, name='dispatch')
class TemplateDeleteView(InvoiceAccessMixin, View):
    def post(self, request, pk):
        tmpl = get_object_or_404(InvoiceTemplate, pk=pk)
        if not tmpl.user_can_edit(request.user):
            messages.error(request, 'You cannot delete this template.')
            return redirect('invoices:template_list')
        name = tmpl.name
        tmpl.delete()
        messages.success(request, f'Template "{name}" deleted. Existing invoices keep their data.')
        return redirect('invoices:template_list')


# ── Conversion (Proforma → Invoice → Delivery Note) ──────────────────────────

class InvoiceConvertView(InvoiceAccessMixin, View):
    """
    GET renders a letterhead-picker modal page (user must confirm or swap
    the letterhead before conversion).
    POST performs the conversion and redirects to the new draft's builder.
    """
    template_name = 'invoices/convert.html'

    def get(self, request, pk):
        source = get_object_or_404(InvoiceDocument, pk=pk)
        if not source.can_convert:
            messages.error(request, 'This document cannot be converted.')
            return redirect('invoices:invoice_detail', pk=source.pk)

        pdfs = _user_accessible_pdfs(request.user).order_by('-created_at')[:80]
        return render(request, self.template_name, {
            'source': source,
            'target_type_display': dict(DocType.choices).get(source.next_doc_type, ''),
            'target_type': source.next_doc_type,
            'pdfs': pdfs,
        })

    def post(self, request, pk):
        source = get_object_or_404(InvoiceDocument, pk=pk)
        letterhead_id = request.POST.get('letterhead_id') or ''
        new_letterhead = None
        if letterhead_id:
            try:
                new_letterhead = _user_accessible_pdfs(request.user).get(pk=letterhead_id)
            except SharedFile.DoesNotExist:
                messages.error(request, 'Selected letterhead is not accessible.')
                return redirect('invoices:invoice_convert', pk=source.pk)

        try:
            child = convert_invoice(source, request.user, new_letterhead=new_letterhead)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('invoices:invoice_detail', pk=source.pk)

        messages.success(request,
            f'Draft {child.get_doc_type_display()} created from {source.number}. '
            f'Review and edit before finalizing.')
        return redirect('invoices:invoice_edit', pk=child.pk)


class ConvertibleSourcesListView(InvoiceAccessMixin, View):
    """
    JSON endpoint returning finalized proformas/invoices that haven't been
    converted yet — powers the "Create from Existing" tab on New Invoice.
    Optional ?target_type= filter ('invoice' or 'delivery_note') narrows to
    sources that convert to that type.
    """
    def get(self, request):
        target = request.GET.get('target_type', '')
        # Only return sources that are:
        #  - finalized
        #  - not already converted (converted_to reverse lookup is empty)
        qs = InvoiceDocument.objects.filter(
            status=InvoiceDocument.Status.FINALIZED,
            converted_to__isnull=True,
        )
        if target == DocType.INVOICE:
            qs = qs.filter(doc_type=DocType.PROFORMA)
        elif target == DocType.DELIVERY_NOTE:
            qs = qs.filter(doc_type=DocType.INVOICE)
        else:
            # Default: show everything that CAN be converted (not delivery notes)
            qs = qs.exclude(doc_type=DocType.DELIVERY_NOTE)

        qs = qs.select_related('created_by')[:100]
        return JsonResponse({
            'sources': [
                {
                    'id': str(d.id),
                    'number': d.number,
                    'doc_type': d.doc_type,
                    'doc_type_display': d.get_doc_type_display(),
                    'next_type_display': dict(DocType.choices).get(d.next_doc_type, ''),
                    'client_name': d.client_name or '—',
                    'total': str(d.total),
                    'currency': d.currency,
                    'invoice_date': d.invoice_date.strftime('%d %b %Y') if d.invoice_date else '',
                    'finalized_at': d.finalized_at.strftime('%d %b %Y') if d.finalized_at else '',
                }
                for d in qs
            ],
        })