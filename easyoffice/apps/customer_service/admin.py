
from django.contrib import admin
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
)


class CustomerPhoneInline(admin.TabularInline):
    model = CustomerPhone
    extra = 1


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("display_name", "customer_type", "email", "preferred_contact_method", "needs_feedback", "created_at")
    search_fields = ("full_name", "company_name", "email", "customer_code")
    inlines = [CustomerPhoneInline]


@admin.register(CallRecord)
class CallRecordAdmin(admin.ModelAdmin):
    list_display = ("phone_number", "customer", "call_type", "handled_by", "started_at", "outcome")
    list_filter = ("call_type", "outcome", "direction")
    search_fields = ("phone_number", "subject", "summary")


@admin.register(SLAPolicy)
class SLAPolicyAdmin(admin.ModelAdmin):
    list_display = ("department", "request_type", "priority", "first_response_minutes", "resolution_hours", "escalation_after_minutes", "is_active")
    list_filter = ("department", "priority", "is_active")


class ServiceTicketUpdateInline(admin.TabularInline):
    model = ServiceTicketUpdate
    extra = 0


class ServiceTicketRoutingInline(admin.TabularInline):
    model = ServiceTicketRouting
    extra = 0


class ServiceTicketAssignmentInline(admin.TabularInline):
    model = ServiceTicketAssignment
    extra = 0


@admin.register(ServiceTicket)
class ServiceTicketAdmin(admin.ModelAdmin):
    list_display = ("ticket_no", "customer", "ticket_type", "priority", "status", "department", "current_owner", "created_at", "resolution_due_at")
    list_filter = ("ticket_type", "priority", "status", "department")
    search_fields = ("ticket_no", "subject", "customer__full_name", "customer__company_name")
    inlines = [ServiceTicketRoutingInline, ServiceTicketAssignmentInline, ServiceTicketUpdateInline]


@admin.register(FeedbackRequest)
class FeedbackRequestAdmin(admin.ModelAdmin):
    list_display = ("ticket", "customer", "channel", "sent_to", "sent_at", "completed_at")


@admin.register(ServiceRating)
class ServiceRatingAdmin(admin.ModelAdmin):
    list_display = ("ticket", "customer", "service_quality", "product_quality", "response_time", "professionalism", "created_at")
    list_filter = ("service_quality", "product_quality", "response_time", "professionalism")
