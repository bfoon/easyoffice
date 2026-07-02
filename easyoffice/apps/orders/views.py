"""
apps/orders/views.py
────────────────────
Internal HTML views — list, detail, create, document generation, cancel,
plus the one-click "Sign with my saved signature" endpoint for the CEO.

Document-generation actions queue a CEO signature request. The CEO can
either go through the regular files-app signing flow (signature pad) OR
hit the new one-click endpoint that stamps their default SavedSignature
at fixed bottom-left coordinates and immediately marks the request signed.
"""
from decimal import Decimal
from io import BytesIO

from django.contrib import messages
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Q, Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.generic import ListView, DetailView, View, TemplateView

from .models import (
    SalesOrder, OrderStatus, OrderSource, PENDING_SIGNATURE_STATUSES,
)
from .forms import OrderHeaderForm, CancelOrderForm, AttachCustomerForm
from .permissions import (
    OrdersAccessMixin, can_fulfill_order, can_cancel_order,
    can_confirm_order, can_sign_orders_documents, find_ceo_user,
)
from . import services


# ── Helpers ────────────────────────────────────────────────────────────────

def _get_active_signature_for_order(order, user):
    """
    Find the open SignatureRequest for the order's current document stage,
    plus — if the current user is the CEO assigned to sign it — return their
    SignatureRequestSigner row so the template can link to the signing page.

    Returns a dict:
        { 'request': SignatureRequest, 'signer': SignatureRequestSigner|None,
          'token': uuid|None, 'sign_url': str|None,
          'has_default_saved_sig': bool }
    or None if no active signature request exists.
    """
    if not order.is_awaiting_signature:
        return None

    try:
        from apps.files.models import SignatureRequest, SignatureRequestSigner, SavedSignature
    except ImportError:
        return None

    stage_key = {
        OrderStatus.PROFORMA_PENDING_SIGNATURE: 'proforma',
        OrderStatus.INVOICE_PENDING_SIGNATURE:  'invoice',
        OrderStatus.DN_PENDING_SIGNATURE:       'delivery note',
    }.get(order.status)

    sig_req = None

    # 1. Match by metadata
    try:
        sig_req = (
            SignatureRequest.objects
            .filter(metadata__contains={'orders.order_id': str(order.pk),
                                         'orders.stage':    stage_key})
            .order_by('-created_at')
            .first()
        )
    except Exception:
        pass

    # 2. Fallback: match via the document's PDF
    if not sig_req:
        current_doc = {
            OrderStatus.PROFORMA_PENDING_SIGNATURE: order.proforma,
            OrderStatus.INVOICE_PENDING_SIGNATURE:  order.invoice,
            OrderStatus.DN_PENDING_SIGNATURE:       order.delivery_note,
        }.get(order.status)
        if current_doc and getattr(current_doc, 'generated_pdf_id', None):
            for field_name in ('document', 'file', 'shared_file', 'attachment'):
                try:
                    sig_req = (
                        SignatureRequest.objects
                        .filter(**{f'{field_name}_id': current_doc.generated_pdf_id})
                        .exclude(status__in=['completed', 'signed', 'done', 'finished'])
                        .order_by('-created_at')
                        .first()
                    )
                    if sig_req:
                        break
                except Exception:
                    continue

    if not sig_req:
        return None

    signer = None
    token = None
    sign_url = None
    has_default_sig = False

    if user and getattr(user, 'is_authenticated', False):
        try:
            signer = (
                SignatureRequestSigner.objects
                .filter(request=sig_req, user=user)
                .exclude(status='signed')
                .order_by('order', 'pk')
                .first()
            )
            if signer:
                token = signer.token
                from django.urls import reverse
                try:
                    sign_url = reverse('sign_document', kwargs={'token': token})
                except Exception:
                    sign_url = None
        except Exception:
            signer = None

        # Does this user have a default saved signature ready to one-click?
        try:
            has_default_sig = SavedSignature.objects.filter(
                user=user, is_default=True,
            ).exists()
        except Exception:
            has_default_sig = False

    return {
        'request':               sig_req,
        'signer':                signer,
        'token':                 token,
        'sign_url':              sign_url,
        'has_default_saved_sig': has_default_sig,
    }


# ── Dashboard ───────────────────────────────────────────────────────────────

class OrdersDashboardView(OrdersAccessMixin, TemplateView):
    template_name = 'orders/dashboard.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        qs = SalesOrder.objects.all()
        ctx['count_new']               = qs.filter(status=OrderStatus.NEW).count()
        ctx['count_confirmed']         = qs.filter(status=OrderStatus.CONFIRMED).count()
        ctx['count_pending_signature'] = qs.filter(status__in=PENDING_SIGNATURE_STATUSES).count()
        ctx['count_in_progress']       = qs.filter(status__in=[
            OrderStatus.PROFORMA_SIGNED, OrderStatus.INVOICE_SIGNED,
        ]).count()
        ctx['count_fulfilled']         = qs.filter(status=OrderStatus.FULFILLED).count()
        ctx['count_cancelled']         = qs.filter(status=OrderStatus.CANCELLED).count()
        ctx['recent_orders']           = qs.select_related('customer').order_by('-created_at')[:15]
        ctx['source_choices']          = OrderSource.choices
        ctx['status_choices']          = OrderStatus.choices
        ctx['ceo_configured']          = find_ceo_user() is not None
        return ctx


# ── List ───────────────────────────────────────────────────────────────────

class OrderListView(OrdersAccessMixin, ListView):
    template_name        = 'orders/list.html'
    context_object_name  = 'orders'
    paginate_by          = 25
    model                = SalesOrder

    def get_queryset(self):
        qs = SalesOrder.objects.select_related('customer', 'invoice', 'proforma', 'delivery_note')
        gp = self.request.GET
        if status := gp.get('status'):
            if status == 'awaiting_signature':
                qs = qs.filter(status__in=PENDING_SIGNATURE_STATUSES)
            else:
                qs = qs.filter(status=status)
        if source := gp.get('source'):
            qs = qs.filter(source=source)
        if q := gp.get('q'):
            qs = qs.filter(
                Q(order_no__icontains=q) |
                Q(customer__full_name__icontains=q) |
                Q(contact_name__icontains=q) |
                Q(contact_phone__icontains=q) |
                Q(external_ref__icontains=q)
            )
        return qs.order_by('-created_at')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['status_choices'] = OrderStatus.choices
        ctx['source_choices'] = OrderSource.choices
        ctx['current_status'] = self.request.GET.get('status', '')
        ctx['current_source'] = self.request.GET.get('source', '')
        ctx['q']              = self.request.GET.get('q', '')
        return ctx


# ── Create ─────────────────────────────────────────────────────────────────

class OrderCreateView(OrdersAccessMixin, TemplateView):
    template_name = 'orders/create.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['header_form']    = OrderHeaderForm(initial={'currency': 'GMD'})
        ctx['source_choices'] = OrderSource.choices
        return ctx

    def post(self, request, *args, **kwargs):
        header = OrderHeaderForm(request.POST)
        if not header.is_valid():
            messages.error(request, 'Please correct the errors below.')
            return self.render_to_response({
                'header_form': header,
                'source_choices': OrderSource.choices,
            })

        items = []
        i = 0
        while True:
            desc    = request.POST.get(f'items-{i}-description', '').strip()
            qty     = request.POST.get(f'items-{i}-quantity', '').strip()
            up      = request.POST.get(f'items-{i}-unit_price', '').strip()
            prod_id = request.POST.get(f'items-{i}-product_id', '').strip()
            if not (desc or qty or up or prod_id):
                if i > 25:
                    break
                i += 1
                continue
            if not desc:
                i += 1
                continue
            try:
                items.append({
                    'description': desc,
                    'quantity':    Decimal(qty or '1'),
                    'unit_price':  Decimal(up or '0'),
                    'product_id':  prod_id or None,
                })
            except Exception:
                messages.error(request, f'Invalid number on row {i + 1}.')
                return self.render_to_response({
                    'header_form': header,
                    'source_choices': OrderSource.choices,
                })
            i += 1

        if not items:
            messages.error(request, 'Add at least one line item.')
            return self.render_to_response({
                'header_form': header,
                'source_choices': OrderSource.choices,
            })

        source = request.POST.get('source', OrderSource.PHONE)
        if source not in {s for s, _ in OrderSource.choices}:
            source = OrderSource.PHONE

        cleaned = header.cleaned_data
        payload = {
            'customer_id':      cleaned['customer'].pk if cleaned.get('customer') else None,
            'contact_name':     cleaned.get('contact_name', ''),
            'contact_phone':    cleaned.get('contact_phone', ''),
            'contact_email':    cleaned.get('contact_email', ''),
            'delivery_address': cleaned.get('delivery_address', ''),
            'notes':            cleaned.get('notes', ''),
            'currency':         cleaned.get('currency', 'GMD'),
            'tax_rate':         cleaned.get('tax_rate', 0),
            'discount_amount':  cleaned.get('discount_amount', 0),
            'items':            items,
        }
        try:
            order = services.create_order_from_payload(payload, source=source, actor=request.user)
        except ValueError as e:
            messages.error(request, str(e))
            return self.render_to_response({
                'header_form': header,
                'source_choices': OrderSource.choices,
            })
        messages.success(request, f'Order {order.order_no} created.')
        return redirect('orders:order_detail', pk=order.pk)


# ── Detail ─────────────────────────────────────────────────────────────────

class OrderDetailView(OrdersAccessMixin, DetailView):
    model               = SalesOrder
    template_name       = 'orders/detail.html'
    context_object_name = 'order'

    def get_queryset(self):
        return SalesOrder.objects.select_related(
            'customer', 'proforma', 'invoice', 'delivery_note',
            'created_by', 'fulfilled_by', 'confirmed_by',
        ).prefetch_related('items', 'events__actor')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        order = self.object
        user  = self.request.user

        ctx['can_confirm'] = (
            order.status == OrderStatus.NEW
            and can_confirm_order(user, order)
        )
        ctx['can_cancel'] = (
            can_cancel_order(user, order)
            and order.status != OrderStatus.FULFILLED
            and order.status != OrderStatus.CANCELLED
        )

        base_can_fulfill = can_fulfill_order(user, order)
        ctx['can_generate_proforma'] = (
            base_can_fulfill
            and order.status == OrderStatus.CONFIRMED
            and order.is_fulfillable
            and not order.proforma_id
        )
        ctx['can_generate_invoice'] = (
            base_can_fulfill
            and order.status == OrderStatus.PROFORMA_SIGNED
            and not order.invoice_id
        )
        ctx['can_generate_delivery_note'] = (
            base_can_fulfill
            and order.status == OrderStatus.INVOICE_SIGNED
            and not order.delivery_note_id
        )

        ctx['pending_signature_doc'] = order.pending_signature_doc_label
        ctx['ceo_user']              = find_ceo_user()
        ctx['can_sign']              = can_sign_orders_documents(user)
        ctx['active_signature']      = _get_active_signature_for_order(order, user)
        ctx['active_signature_request'] = (
            ctx['active_signature']['request'] if ctx['active_signature'] else None
        )

        # URL to the saved-signatures management page (so CEO can save one
        # before using one-click sign).
        from django.urls import reverse, NoReverseMatch
        try:
            ctx['saved_signatures_url'] = reverse('saved_signatures')
        except NoReverseMatch:
            ctx['saved_signatures_url'] = ''

        ctx['attach_customer_form'] = AttachCustomerForm()
        ctx['cancel_form']          = CancelOrderForm()
        return ctx

    def post(self, request, *args, **kwargs):
        # Browsers sometimes replay an old POST against this URL when the
        # user hits Refresh after a Post-Redirect-Get cycle. The proper
        # answer is "look, here's the current page" — not a 405. Redirect
        # back to the canonical GET so the order detail just re-renders.
        return redirect('orders:order_detail', pk=kwargs.get('pk'))


# ── Action endpoints ───────────────────────────────────────────────────────

class OrderConfirmView(OrdersAccessMixin, View):
    """POST /<pk>/confirm/ — Sales Supervisor / Manager / Admin / CEO only."""
    def post(self, request, pk):
        order = get_object_or_404(SalesOrder, pk=pk)
        if not can_confirm_order(request.user, order):
            messages.error(
                request,
                'Only a Sales Supervisor, Manager, Admin or CEO can confirm orders.',
            )
            return redirect('orders:order_detail', pk=order.pk)
        try:
            services.confirm_order(order, request.user)
        except ValueError as e:
            messages.warning(request, str(e))
            return redirect('orders:order_detail', pk=order.pk)
        messages.success(
            request,
            f'Order {order.order_no} confirmed. Customer notified — '
            f'a team member can now generate the proforma.',
        )
        return redirect('orders:order_detail', pk=order.pk)


class OrderGenerateProformaView(OrdersAccessMixin, View):
    """POST /<pk>/generate-proforma/ → builds Proforma + queues CEO signature."""
    def post(self, request, pk):
        order = get_object_or_404(SalesOrder, pk=pk)
        if not can_fulfill_order(request.user, order):
            messages.error(request, "You don't have permission to generate documents.")
            return redirect('orders:order_detail', pk=order.pk)
        try:
            order = services.generate_proforma_for_order(order, request.user)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('orders:order_detail', pk=order.pk)
        except Exception as e:  # noqa: BLE001
            messages.error(request, f'Could not generate proforma: {e}')
            return redirect('orders:order_detail', pk=order.pk)

        order = SalesOrder.objects.select_related('proforma').get(pk=order.pk)
        messages.success(
            request,
            f'Proforma {order.proforma.number} generated and sent to the CEO '
            f'for signature. The buyer will be emailed once it is signed.',
        )
        return redirect('orders:order_detail', pk=order.pk)


class OrderGenerateInvoiceView(OrdersAccessMixin, View):
    """POST /<pk>/generate-invoice/ → only valid AFTER proforma is signed."""
    def post(self, request, pk):
        order = get_object_or_404(SalesOrder, pk=pk)
        if not can_fulfill_order(request.user, order):
            messages.error(request, "You don't have permission to generate invoices.")
            return redirect('orders:order_detail', pk=order.pk)
        try:
            order = services.generate_invoice_for_order(order, request.user)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('orders:order_detail', pk=order.pk)
        except Exception as e:  # noqa: BLE001
            messages.error(request, f'Could not generate invoice: {e}')
            return redirect('orders:order_detail', pk=order.pk)

        order = SalesOrder.objects.select_related('invoice').get(pk=order.pk)
        messages.success(
            request,
            f'Invoice {order.invoice.number} generated and sent to the CEO '
            f'for signature.',
        )
        return redirect('orders:order_detail', pk=order.pk)


class OrderGenerateDeliveryNoteView(OrdersAccessMixin, View):
    """POST /<pk>/generate-delivery-note/ → only valid AFTER invoice is signed."""
    def post(self, request, pk):
        order = get_object_or_404(SalesOrder, pk=pk)
        if not can_fulfill_order(request.user, order):
            messages.error(request, "You don't have permission to generate delivery notes.")
            return redirect('orders:order_detail', pk=order.pk)
        try:
            order = services.generate_delivery_note_for_order(order, request.user)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('orders:order_detail', pk=order.pk)
        except Exception as e:  # noqa: BLE001
            messages.error(request, f'Could not generate delivery note: {e}')
            return redirect('orders:order_detail', pk=order.pk)

        order = SalesOrder.objects.select_related('delivery_note').get(pk=order.pk)
        messages.success(
            request,
            f'Delivery Note {order.delivery_note.number} generated and sent '
            f'to the CEO for signature. Once signed, the order will be '
            f'marked fulfilled.',
        )
        return redirect('orders:order_detail', pk=order.pk)


class OrderCancelView(OrdersAccessMixin, View):
    def post(self, request, pk):
        order = get_object_or_404(SalesOrder, pk=pk)
        if not can_cancel_order(request.user, order):
            messages.error(request, "You don't have permission to cancel orders.")
            return redirect('orders:order_detail', pk=order.pk)
        form = CancelOrderForm(request.POST)
        reason = form.cleaned_data.get('reason', '') if form.is_valid() else ''
        try:
            services.cancel_order(order, request.user, reason=reason)
        except ValueError as e:
            messages.error(request, str(e))
            return redirect('orders:order_detail', pk=order.pk)
        messages.success(request, f'Order {order.order_no} cancelled.')
        return redirect('orders:order_detail', pk=order.pk)


class OrderAttachCustomerView(OrdersAccessMixin, View):
    def post(self, request, pk):
        order = get_object_or_404(SalesOrder, pk=pk)
        form = AttachCustomerForm(request.POST)
        if not form.is_valid():
            messages.error(request, 'Pick a customer to attach.')
            return redirect('orders:order_detail', pk=order.pk)
        services.attach_customer(order, form.cleaned_data['customer'], request.user)
        messages.success(request, 'Customer attached.')
        return redirect('orders:order_detail', pk=order.pk)


# ════════════════════════════════════════════════════════════════════════════
# One-click CEO sign
# ════════════════════════════════════════════════════════════════════════════

class OrderQuickCEOSignView(OrdersAccessMixin, View):
    """
    POST /<pk>/sign-with-saved/ — CEO one-click sign.

    Reads the CEO's default SavedSignature, stamps it onto the pending
    document at fixed bottom-left coordinates (matching the area where the
    CEO's wet-ink signature would go on the company's letterhead), saves
    the result back to the document's SharedFile, and marks the
    SignatureRequest completed. The existing post-save signal in
    apps/orders/signals.py then fires services.on_*_signed which emails
    the buyer (and logistics, on the DN stage).

    Requires: reportlab + Pillow + pypdf (already in your stack — used by
    QuickSignView).
    """
    def post(self, request, pk):
        order = get_object_or_404(SalesOrder, pk=pk)

        if not can_sign_orders_documents(request.user):
            messages.error(request, 'Only the CEO can sign order documents.')
            return redirect('orders:order_detail', pk=order.pk)

        active = _get_active_signature_for_order(order, request.user)
        if not active or not active['request']:
            messages.error(request, 'There is no document awaiting your signature for this order.')
            return redirect('orders:order_detail', pk=order.pk)

        sig_req = active['request']
        signer  = active['signer']
        if not signer:
            messages.error(
                request,
                'You are not the assigned signer for this document. Use the '
                'normal signing flow.',
            )
            return redirect('orders:order_detail', pk=order.pk)

        # Get CEO's default saved signature
        try:
            from apps.files.models import SavedSignature
        except ImportError:
            messages.error(request, 'Saved signatures are not available in this environment.')
            return redirect('orders:order_detail', pk=order.pk)

        saved_sig = SavedSignature.objects.filter(
            user=request.user, is_default=True,
        ).first() or SavedSignature.objects.filter(user=request.user).first()

        if not saved_sig:
            messages.error(
                request,
                'You have no saved signature. Save one in '
                '"Saved Signatures" first, then come back to one-click sign.',
            )
            return redirect('orders:order_detail', pk=order.pk)

        # Resolve signature data — either base64 PNG (drawn/typed-as-image)
        # or an uploaded image file.
        sig_data_uri = None
        sig_image_bytes = None
        if saved_sig.image and getattr(saved_sig.image, 'name', ''):
            try:
                with saved_sig.image.open('rb') as fh:
                    sig_image_bytes = fh.read()
            except Exception:
                sig_image_bytes = None
        if not sig_image_bytes:
            sig_data_uri = (saved_sig.data or '').strip()
        if not sig_image_bytes and not sig_data_uri:
            # No usable image in the saved signature (e.g. typed-only).
            # Proceed anyway — the stamper falls back to drawing the CEO's
            # name in script style so a signature is always visible.
            sig_data_uri = None

        # ── Stamp the PDF ──────────────────────────────────────────────────
        try:
            self._stamp_and_complete(
                request=request, order=order, sig_req=sig_req, signer=signer,
                sig_data_uri=sig_data_uri, sig_image_bytes=sig_image_bytes,
            )
        except Exception as e:  # noqa: BLE001
            messages.error(request, f'Could not sign document: {e}')
            return redirect('orders:order_detail', pk=order.pk)

        messages.success(
            request,
            f'Document signed. The buyer has been emailed the signed copy.',
        )
        return redirect('orders:order_detail', pk=order.pk)

    @transaction.atomic
    def _stamp_and_complete(self, *, request, order, sig_req, signer,
                            sig_data_uri, sig_image_bytes):
        """
        Composite the saved signature onto the document, save the new PDF
        to the same SharedFile, and complete the SignatureRequest.

        Placement is layout-aware: right side of the LAST page, below the
        totals block and clear of the notes block, computed from the
        document's actual layout_json (services.compute_ceo_signature_spot).
        A signature is ALWAYS rendered — if the saved signature has no
        usable image, the CEO's name is drawn in script style instead.
        """
        # Late imports — same libs the files-app QuickSignView uses
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.lib.utils import ImageReader
        import base64
        from datetime import datetime as _dt

        document = sig_req.document
        if not document or not getattr(document, 'file', None):
            raise RuntimeError('Document is missing its PDF file.')

        # Read the source PDF
        with document.file.open('rb') as fh:
            src_bytes = fh.read()
        reader = PdfReader(BytesIO(src_bytes))
        writer = PdfWriter()

        # ── Decode the saved signature into image bytes (robustly) ─────────
        # Handles: data URIs, raw base64 blobs without the data: prefix,
        # and uploaded image files. If none of those yields an image (e.g.
        # a typed-text signature saved without a rendered image), we fall
        # back to DRAWING the CEO's name in an italic script style — the
        # document must never come back "signed" with nothing visible.
        if sig_image_bytes is None and sig_data_uri:
            b64 = sig_data_uri
            if b64.startswith('data:image'):
                b64 = b64.split(',', 1)[1]
            try:
                candidate = base64.b64decode(b64, validate=False)
                # PNG / JPEG / GIF magic check
                if candidate[:4] in (b'\x89PNG',) or candidate[:3] == b'\xff\xd8\xff' \
                        or candidate[:4] == b'GIF8':
                    sig_image_bytes = candidate
            except Exception:
                sig_image_bytes = None

        # ── Placement — layout-aware, content-safe ─────────────────────────
        # Computed from the document's actual block layout (right side,
        # below the totals block, clear of the notes block). Falls back to
        # a safe fixed spot if the invoice record can't be resolved.
        from .services import compute_ceo_signature_spot
        invoice_doc = None
        try:
            meta = getattr(sig_req, 'metadata', None) or {}
            inv_id = meta.get('orders.invoice_id')
            if inv_id:
                from apps.invoices.models import InvoiceDocument
                invoice_doc = InvoiceDocument.objects.filter(pk=inv_id).first()
            if invoice_doc is None:
                for candidate_doc in (order.delivery_note, order.invoice, order.proforma):
                    if candidate_doc is not None and candidate_doc.generated_pdf_id == document.pk:
                        invoice_doc = candidate_doc
                        break
        except Exception:
            invoice_doc = None

        if invoice_doc is not None:
            spot = compute_ceo_signature_spot(invoice_doc)
        else:
            spot = {'x_pct': 62.0, 'y_pct': 80.0, 'w_pct': 20.0, 'h_pct': 6.0}

        SIG_X_PCT = spot['x_pct']
        SIG_Y_PCT = spot['y_pct']      # measured from top
        SIG_W_PCT = spot['w_pct']
        SIG_H_PCT = spot['h_pct']      # includes the caption lines

        signed_at = _dt.now().strftime('%d %b %Y %H:%M UTC')
        full_name = (
            getattr(request.user, 'full_name', None)
            or request.user.get_full_name()
            or request.user.username
        )

        # Totals / bank details / notes always render on the LAST page of a
        # multi-page document — the signature belongs next to them, not on
        # an items-only first page.
        stamp_page_idx = len(reader.pages) - 1

        for page_idx in range(len(reader.pages)):
            page = reader.pages[page_idx]
            page_w = float(page.mediabox.width)
            page_h = float(page.mediabox.height)

            overlay = BytesIO()
            c = rl_canvas.Canvas(overlay, pagesize=(page_w, page_h))

            if page_idx == stamp_page_idx:
                fx = (SIG_X_PCT / 100.0) * page_w
                fw = (SIG_W_PCT / 100.0) * page_w
                box_h = (SIG_H_PCT / 100.0) * page_h
                box_top_y = page_h * (1.0 - SIG_Y_PCT / 100.0)

                caption_h = 16          # two caption lines in points
                img_h = max(14, box_h - caption_h)
                img_y = box_top_y - img_h

                if sig_image_bytes:
                    try:
                        c.drawImage(
                            ImageReader(BytesIO(sig_image_bytes)),
                            fx, img_y, width=fw, height=img_h,
                            preserveAspectRatio=True, anchor='sw', mask='auto',
                        )
                    except Exception:
                        sig_image_bytes = None  # fall through to typed fallback

                if not sig_image_bytes:
                    # Typed fallback — the name in an italic script style,
                    # with a signing line, so the signature is ALWAYS visible.
                    c.setFont('Helvetica-Oblique', 16)
                    c.setFillColorRGB(0.10, 0.15, 0.35)
                    c.drawString(fx + 2, img_y + img_h * 0.35, full_name)
                    c.setLineWidth(0.7)
                    c.setStrokeColorRGB(0.35, 0.40, 0.50)
                    c.line(fx, img_y + img_h * 0.22, fx + fw, img_y + img_h * 0.22)

                # Caption under the signature
                c.setFont('Helvetica', 7.5)
                c.setFillColorRGB(0.20, 0.30, 0.45)
                c.drawString(fx, max(2, img_y - 8), f'CEO · {full_name}')
                c.setFont('Helvetica', 6.5)
                c.setFillColorRGB(0.45, 0.50, 0.58)
                c.drawString(fx, max(2, img_y - 16), f'Electronically signed · {signed_at}')

            c.save()
            overlay.seek(0)
            page.merge_page(PdfReader(overlay).pages[0])
            writer.add_page(page)

        out = BytesIO()
        writer.write(out)
        signed_bytes = out.getvalue()

        # ── Replace the SharedFile with the signed bytes ──
        # Same file record (so the SignatureRequest.document FK stays valid).
        # Bump filename + reset hash so file_hash recomputes.
        original_name = document.name or 'document.pdf'
        if not original_name.lower().endswith('.pdf'):
            original_name += '.pdf'

        document.file.save(original_name, ContentFile(signed_bytes), save=False)
        document.file_size = len(signed_bytes)
        document.file_type = 'application/pdf'
        try:
            document.file_hash = document.compute_hash()
        except Exception:
            document.file_hash = ''
        document.save(update_fields=['file', 'file_size', 'file_type', 'file_hash'])

        # ── Mark the signer as signed ──
        signer.status = 'signed'
        signer.signed_at = timezone.now()
        if hasattr(signer, 'signature_data'):
            signer.signature_data = sig_data_uri or ''
        if hasattr(signer, 'signature_type'):
            signer.signature_type = saved_sig_type_for(sig_image_bytes, sig_data_uri)
        if hasattr(signer, 'ip_address'):
            signer.ip_address = request.META.get('REMOTE_ADDR', '') or ''
        if hasattr(signer, 'user_agent'):
            signer.user_agent = (request.META.get('HTTP_USER_AGENT', '') or '')[:500]
        signer.save()

        # ── Audit event ──
        try:
            from apps.files.models import SignatureAuditEvent
            SignatureAuditEvent.objects.create(
                request=sig_req,
                event='signed',
                signer_email=signer.email,
                signer_name=signer.name or full_name,
                notes=f'One-click sign with saved signature (Order {order.order_no})',
            )
        except Exception:
            pass

        # ── Complete the request — this fires the orders post_save signal ──
        # We update_status() if available; otherwise set the field directly.
        if hasattr(sig_req, 'update_status'):
            sig_req.update_status()
        else:
            sig_req.status = 'completed'
            sig_req.completed_at = timezone.now()
            sig_req.save(update_fields=['status', 'completed_at', 'updated_at'])

        try:
            sig_req.rebuild_audit_hash()
        except Exception:
            pass


def saved_sig_type_for(image_bytes, data_uri):
    """Best-effort SignatureRequestSigner.signature_type value."""
    if image_bytes:
        return 'upload' if not (data_uri or '').startswith('data:image') else 'draw'
    if (data_uri or '').startswith('data:image'):
        return 'draw'
    return 'type'

# ════════════════════════════════════════════════════════════════════════
# Inventory typeahead — feeds the line-item searchable dropdown
# ════════════════════════════════════════════════════════════════════════

class SellableProductsAPIView(OrdersAccessMixin, View):
    """
    GET /orders/api/sellable-products/?q=<search>

    Returns up to 20 inventory.Product rows that:
        • are flagged sellable + active
        • are kind=STOCKED (services and bundles never appear)

    Out-of-stock products ARE included — they can still be ordered; stock
    deduction at fulfillment is clamped to what's on hand. Each row carries
    `available` and `stock_status` ('ok' / 'low' / 'out') so the picker can
    badge them.

    Used by the line-item picker on the order create page. Search matches
    SKU, name, or barcode (case-insensitive). Inventory app must be
    installed; if it isn't, returns an empty list rather than 500.
    """

    def get(self, request):
        q = (request.GET.get('q') or '').strip()

        try:
            from apps.inventory.models import Product
        except Exception:
            return JsonResponse({'results': [], 'reason': 'inventory_app_unavailable'})

        from django.db.models import Sum, F, DecimalField, Value
        from django.db.models.functions import Coalesce
        from decimal import Decimal as D

        # Aggregate stock in ONE query so we don't trigger 60 sub-queries
        # via the @property accessors.
        zero = Value(D('0'), output_field=DecimalField(max_digits=14, decimal_places=2))
        qs = (Product.objects
              .filter(is_sellable=True, is_active=True,
                      kind=Product.Kind.STOCKED)
              .annotate(
                  on_hand_total=Coalesce(Sum('stock_items__quantity'), zero),
                  reserved_total=Coalesce(Sum('stock_items__reserved_quantity'), zero),
              )
              .annotate(available=F('on_hand_total') - F('reserved_total'))
              .select_related('category')
              .order_by('name'))

        if q:
            qs = qs.filter(Q(sku__icontains=q) |
                           Q(name__icontains=q) |
                           Q(barcode__icontains=q))

        results = []
        for p in qs[:20]:
            avail = p.available or D('0')
            if avail <= 0:
                stock_status = 'out'
            elif avail < (p.reorder_point or D('0')):
                stock_status = 'low'
            else:
                stock_status = 'ok'
            results.append({
                'id':           str(p.pk),
                'sku':          p.sku,
                'name':         p.name,
                'unit':         getattr(p, 'unit_label', '') or '',
                'currency':     p.currency,
                'sell_price':   str(p.sell_price or '0'),
                'available':    str(avail),
                'stock_status': stock_status,
                'category':     p.category.name if p.category_id else '',
            })
        return JsonResponse({'results': results})


class CustomerSearchAPIView(OrdersAccessMixin, View):
    """
    GET /orders/api/customers/?q=<search>

    Typeahead for the order create page's Contact name field. Matches
    existing customers by name, email, or phone number (case-insensitive)
    and returns everything needed to auto-fill the contact snapshot:
    name, primary phone, email, and address.
    """

    def get(self, request):
        q = (request.GET.get('q') or '').strip()
        if len(q) < 2:
            return JsonResponse({'results': []})

        try:
            from apps.customer_service.models import Customer
        except Exception:
            return JsonResponse({'results': [], 'reason': 'customer_service_unavailable'})

        from .services import _customer_primary_phone

        qs = (
            Customer.objects
            .filter(
                Q(full_name__icontains=q) |
                Q(email__icontains=q) |
                Q(phones__phone_number__icontains=q)
            )
            .distinct()
            .order_by('full_name')[:10]
        )

        results = []
        for c in qs:
            results.append({
                'id':      str(c.pk),
                'name':    getattr(c, 'full_name', '') or getattr(c, 'display_name', '') or '',
                'email':   getattr(c, 'email', '') or '',
                'phone':   _customer_primary_phone(c),
                'address': getattr(c, 'address', '') or '',
            })
        return JsonResponse({'results': results})