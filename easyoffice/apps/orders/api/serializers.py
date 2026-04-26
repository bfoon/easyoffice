"""
apps/orders/api/serializers.py
──────────────────────────────
DRF serializers for the public orders endpoint.

`OrderItemInputSerializer`     — line item input
`OrderCreateSerializer`        — input wrapper for POST /api/orders/
`OrderSerializer`              — read-only output (returned after creation)
"""
from decimal import Decimal
from rest_framework import serializers

from ..models import SalesOrder, SalesOrderItem


class OrderItemInputSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=300)
    quantity    = serializers.DecimalField(max_digits=10, decimal_places=2,
                                            min_value=Decimal('0.01'))
    unit_price  = serializers.DecimalField(max_digits=12, decimal_places=2,
                                            min_value=Decimal('0'))


class OrderCreateSerializer(serializers.Serializer):
    """
    Inbound shape for POST /api/orders/.

    Either `customer_id` (preferred) or `contact_phone`/`contact_email`
    (so we can auto-link or auto-create) must be present.
    """
    customer_id      = serializers.IntegerField(required=False, allow_null=True)
    contact_name     = serializers.CharField(max_length=180, required=False, allow_blank=True)
    contact_phone    = serializers.CharField(max_length=30,  required=False, allow_blank=True)
    contact_email    = serializers.EmailField(            required=False, allow_blank=True)
    delivery_address = serializers.CharField(required=False, allow_blank=True)
    notes            = serializers.CharField(required=False, allow_blank=True)
    currency         = serializers.CharField(max_length=3, required=False, default='GMD')
    tax_rate         = serializers.DecimalField(max_digits=5, decimal_places=2,
                                                  required=False, default=Decimal('0'),
                                                  min_value=Decimal('0'),
                                                  max_value=Decimal('100'))
    discount_amount  = serializers.DecimalField(max_digits=12, decimal_places=2,
                                                  required=False, default=Decimal('0'),
                                                  min_value=Decimal('0'))
    external_ref     = serializers.CharField(max_length=80, required=False, allow_blank=True)
    idempotency_key  = serializers.CharField(max_length=80, required=False, allow_blank=True)
    items            = OrderItemInputSerializer(many=True, min_length=1)

    def validate(self, attrs):
        # Need either a customer_id or some way to identify them
        if not attrs.get('customer_id'):
            if not (attrs.get('contact_phone') or attrs.get('contact_email')):
                raise serializers.ValidationError(
                    'Provide either customer_id or at least one of contact_phone / contact_email.'
                )
        return attrs


# ── Read serializers ────────────────────────────────────────────────────────

class OrderItemOutputSerializer(serializers.ModelSerializer):
    line_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model  = SalesOrderItem
        fields = ['id', 'position', 'description', 'quantity', 'unit_price', 'line_total']


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemOutputSerializer(many=True, read_only=True)
    customer_id   = serializers.IntegerField(source='customer.pk', read_only=True, allow_null=True)
    customer_name = serializers.CharField(source='customer.display_name', read_only=True, allow_null=True)
    proforma_no      = serializers.CharField(source='proforma.number',      read_only=True, allow_null=True)
    invoice_no       = serializers.CharField(source='invoice.number',       read_only=True, allow_null=True)
    delivery_note_no = serializers.CharField(source='delivery_note.number', read_only=True, allow_null=True)

    class Meta:
        model  = SalesOrder
        fields = [
            'id', 'order_no', 'source', 'external_ref',
            'status',
            'customer_id', 'customer_name',
            'contact_name', 'contact_phone', 'contact_email',
            'delivery_address', 'notes',
            'currency', 'tax_rate', 'discount_amount',
            'subtotal', 'tax_amount', 'total',
            'items',
            'proforma_no', 'invoice_no', 'delivery_note_no',
            'created_at', 'updated_at', 'fulfilled_at',
        ]
        read_only_fields = fields
