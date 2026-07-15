"""
apps/customer_service/customer_detail_patch.py
===============================================

DROP-IN PATCH — this is not a standalone module. Apply the change below to
your existing apps/customer_service/views.py.

Why: CustomerDetailView shows a contact's calls, tickets and ratings, but
equipment maintenance was invisible there — so a rep on the phone couldn't
see the laptop the same customer dropped off last week. Now that
MaintenanceJob has a `customer` FK, the history is one query away.

The import is lazy and wrapped: customer_service must keep working if
it_support isn't installed.


────────────────────────────────────────────────────────────────────────────
STEP 1 — add this helper near the top of apps/customer_service/views.py
────────────────────────────────────────────────────────────────────────────

def _maintenance_jobs_for(customer, limit=20):
    \"\"\"
    Equipment maintenance history for a contact.

    Lazy import: it_support is an optional sibling app. Returns an empty list
    rather than raising if it isn't installed.
    \"\"\"
    try:
        from apps.it_support.models import MaintenanceJob
    except Exception:
        return []
    return list(
        MaintenanceJob.objects
        .filter(customer=customer)
        .select_related('assigned_to', 'invoice', 'receipt')
        .order_by('-created_at')[:limit]
    )


────────────────────────────────────────────────────────────────────────────
STEP 2 — in CustomerDetailView.get_context_data, add the two lines marked +
────────────────────────────────────────────────────────────────────────────

class CustomerDetailView(LoginRequiredMixin, DetailView):
    model = Customer
    template_name = "customer_service/customer_detail.html"
    context_object_name = "customer"

    def dispatch(self, request, *args, **kwargs):
        if not _can_view_contacts(request.user):
            return HttpResponseForbidden("You do not have permission to view contacts.")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        customer = self.get_object()
        ctx["phones"] = customer.phones.filter(is_active=True)
        ctx["calls"] = customer.calls.select_related("handled_by").order_by("-started_at")[:20]
        ctx["tickets"] = customer.tickets.select_related("department", "current_owner").order_by("-created_at")[:20]
        ctx["ratings"] = customer.ratings.order_by("-created_at")[:10]
+       ctx["maintenance_jobs"] = _maintenance_jobs_for(customer)
        return ctx


────────────────────────────────────────────────────────────────────────────
STEP 3 — add this block to customer_service/customer_detail.html
────────────────────────────────────────────────────────────────────────────
Place it alongside the existing calls / tickets cards.

{% if maintenance_jobs %}
<div class="eo-card mb-4">
  <div class="eo-card-header">
    <div>
      <h2 class="eo-card-title">Equipment Maintenance</h2>
      <p class="eo-card-subtitle">Repairs and services handled by IT for this customer.</p>
    </div>
  </div>
  <div class="table-responsive">
    <table class="eo-table mb-0">
      <thead>
        <tr>
          <th>Reference</th><th>Equipment</th><th>Status</th>
          <th>Received</th><th>Technician</th><th class="text-end">Total</th><th></th>
        </tr>
      </thead>
      <tbody>
      {% for job in maintenance_jobs %}
        <tr>
          <td class="fw-semibold">{{ job.maintenance_number }}</td>
          <td>
            {{ job.equipment_name }}
            {% if job.serial_number %}<br><small class="text-muted">S/N {{ job.serial_number }}</small>{% endif %}
          </td>
          <td>
            <span style="background:{{ job.status_color }}18;color:{{ job.status_color }};padding:4px 9px;border-radius:999px;font-size:.75rem;font-weight:700">
              {{ job.get_status_display }}
            </span>
          </td>
          <td>{{ job.created_at|date:"d M Y" }}</td>
          <td>{% if job.assigned_to %}{{ job.assigned_to.full_name }}{% else %}<span class="text-muted">Unassigned</span>{% endif %}</td>
          <td class="text-end">
            {{ job.total_amount }} {{ job.currency }}
            <br><small class="text-muted">{{ job.get_payment_status_display }}</small>
          </td>
          <td class="text-end">
            <a href="{% url 'maintenance_detail' job.pk %}" class="eo-btn eo-btn-secondary eo-btn-sm">Open</a>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% endif %}
"""
