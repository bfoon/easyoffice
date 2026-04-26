"""
apps/orders/api/views.py
────────────────────────
Public API for sales orders.

Endpoints:
    POST /api/orders/                — Create an order from website / integration
    GET  /api/orders/<id>/           — Read order status (auth'd users only)

Authentication is permissive — any of:
    • Api-Key header     (this app's ApiKeyAuthentication)
    • DRF Token          (rest_framework.authtoken.TokenAuthentication)
    • JWT                (rest_framework_simplejwt.authentication.JWTAuthentication)

The ordering matters: we put ApiKey FIRST because the website is the
biggest expected caller. Each class returns None on miss, letting DRF
try the next.
"""
from django.db import IntegrityError
from rest_framework import status, generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.authentication import (
    TokenAuthentication, SessionAuthentication,
)
from rest_framework_simplejwt.authentication import JWTAuthentication

from ..models import SalesOrder, OrderSource
from ..services import create_order_from_payload
from .authentication import ApiKeyAuthentication
from .serializers import OrderCreateSerializer, OrderSerializer


# Ordered so the cheapest / most-likely-used auth is tried first.
_AUTH_CLASSES = [
    ApiKeyAuthentication,
    TokenAuthentication,
    JWTAuthentication,
    SessionAuthentication,  # so the agent can also poke this from the browser
]


class CreateOrderView(generics.CreateAPIView):
    """
    POST /api/orders/

    Headers (pick one):
        Authorization: Api-Key sk_xxxxxxxxxxxxxx
        Authorization: Token <drf-token>
        Authorization: Bearer <jwt-access-token>

    Body (JSON):
        {
          "external_ref": "WEB-CHK-123",
          "idempotency_key": "WEB-CHK-123",
          "contact_name": "Awa Jallow",
          "contact_phone": "+220 999 1234",
          "contact_email": "awa@example.com",
          "delivery_address": "12 Atlantic Road, Banjul",
          "currency": "GMD",
          "tax_rate": "15",
          "items": [
            {"description": "Solar panel 250W", "quantity": "2", "unit_price": "8500"},
            {"description": "Installation",     "quantity": "1", "unit_price": "1500"}
          ]
        }

    Response (201):
        Full order representation including the generated order_no. The
        agent will fulfill it later from the internal app.
    """
    authentication_classes = _AUTH_CLASSES
    permission_classes     = [IsAuthenticated]
    serializer_class       = OrderCreateSerializer

    def create(self, request, *args, **kwargs):
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        payload = dict(ser.validated_data)

        # Source: 'api' if it came in via API key / token, 'website' if the
        # user explicitly identified themselves as the website. We default
        # to 'api' and let the integration override via a query param if
        # they want their orders to show as 'website' in reports.
        source_param = request.query_params.get('source', '').lower()
        if source_param in {s for s, _ in OrderSource.choices}:
            source = source_param
        else:
            source = OrderSource.API

        # Idempotency: if a key is provided and we already have an order
        # with it, return the existing one instead of double-creating.
        idem_key = payload.get('idempotency_key')
        if idem_key:
            existing = SalesOrder.objects.filter(idempotency_key=idem_key).first()
            if existing:
                return Response(
                    OrderSerializer(existing).data,
                    status=status.HTTP_200_OK,
                    headers={'Idempotent-Replay': 'true'},
                )

        try:
            order = create_order_from_payload(payload, source=source, actor=request.user)
        except IntegrityError:
            # Race: another request with the same idempotency_key won the
            # insert between our check and ours. Fetch and return that one.
            if idem_key:
                existing = SalesOrder.objects.filter(idempotency_key=idem_key).first()
                if existing:
                    return Response(OrderSerializer(existing).data, status=status.HTTP_200_OK)
            raise

        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)


class OrderDetailAPIView(generics.RetrieveAPIView):
    """GET /api/orders/<uuid:pk>/  — read order status from the integration side."""
    authentication_classes = _AUTH_CLASSES
    permission_classes     = [IsAuthenticated]
    serializer_class       = OrderSerializer
    queryset               = SalesOrder.objects.all()
    lookup_field           = 'pk'
