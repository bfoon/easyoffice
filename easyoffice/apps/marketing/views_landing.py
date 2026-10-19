"""
apps/marketing/views_landing.py
===============================

Public landing page for easyoffice.gm (Easy Solutions + EasyOffice).

  - LandingView         GET  /                 → public landing page
                                                 (signed-in users go to the dashboard)
  - LandingEnquiryView  POST /enquiry/         → website contact form; creates a
                                                 MarketingContact (stage=lead,
                                                 source=website) so the Marketing
                                                 team picks it up in EasyOffice.

Client logos
------------
Drop approved logo files into  static/landing/clients/<key>.png  (or .svg / .webp),
e.g. static/landing/clients/undp.png, then run collectstatic. A tile only shows
an image when the file exists; otherwise it shows the organisation's name.
(We check existence here because CompressedManifestStaticFilesStorage raises on
{% static %} for files that are not in the manifest.)

Optional settings
-----------------
    LANDING_ENQUIRY_EMAIL = 'sales@yourdomain.gm'   # also email each enquiry here
"""
import logging
import re

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import redirect
from django.views.generic import TemplateView, View

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

# Clients shown in "Organisations we have delivered for".
CLIENTS = [
    ('undp',   'UNDP',   'UN Development Programme'),
    ('fao',    'FAO',    'Food and Agriculture Organization'),
    ('undss',  'UNDSS',  'UN Department of Safety and Security'),
    ('unicef', 'UNICEF', 'UN Children’s Fund'),
    ('moh',    'MOH',    'Ministry of Health'),
    ('oic',    'OIC',    'Organisation of Islamic Cooperation'),
    ('ndma',   'NDMA',   'National Disaster Management Agency'),
    ('alfo',   'ALFO',   ''),
]

# Values allowed from the "What do you need?" picker.
SERVICE_CHOICES = [
    ('dashboards', 'Dashboards & data boards'),
    ('software',   'Custom software'),
    ('easyoffice', 'EasyOffice demo'),
    ('security',   'Security systems'),
    ('it',         'IT systems & networks'),
    ('electronics', 'Electronics purchase'),
    ('other',      'Something else'),
]
SERVICE_LABELS = dict(SERVICE_CHOICES)

THROTTLE_MAX = 5           # enquiries …
THROTTLE_WINDOW = 60 * 60  # … per hour per IP


def _client_ip(request):
    fwd = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def _logo_url(key):
    for ext in ('svg', 'png', 'webp', 'jpg'):
        path = f'landing/clients/{key}.{ext}'
        try:
            if staticfiles_storage.exists(path):
                return staticfiles_storage.url(path)
        except Exception:  # missing manifest entry, storage misconfig, etc.
            continue
    return ''


class LandingView(TemplateView):
    template_name = 'landing/index.html'

    def get(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            # Hard path on purpose: 'dashboard' is named in both the '' and
            # 'dashboard/' includes, and reverse() resolves to '/', which is
            # this view — redirecting by name would loop.
            return redirect('/dashboard/')
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['clients'] = [
            {'key': k, 'name': n, 'full': f, 'logo_url': _logo_url(k)}
            for k, n, f in CLIENTS
        ]
        ctx['service_choices'] = SERVICE_CHOICES
        return ctx


class LandingEnquiryView(View):
    """Public contact form. Returns JSON for the landing page's fetch() call."""

    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        # Honeypot: real visitors never fill this hidden field.
        if request.POST.get('website', '').strip():
            return JsonResponse({'ok': True})

        ip = _client_ip(request)
        key = f'landing_enquiry:{ip}'
        count = cache.get(key, 0)
        if count >= THROTTLE_MAX:
            return JsonResponse(
                {'ok': False, 'error': 'Too many requests from this connection. '
                                       'Try again in an hour or call us directly.'},
                status=429,
            )

        full_name = request.POST.get('name', '').strip()[:200]
        company = request.POST.get('organisation', '').strip()[:200]
        email = request.POST.get('email', '').strip()[:254]
        phone = request.POST.get('phone', '').strip()[:50]
        service = request.POST.get('service', '').strip()
        message = request.POST.get('message', '').strip()[:4000]

        errors = {}
        if not full_name:
            errors['name'] = 'Enter your name.'
        if not EMAIL_RE.match(email):
            errors['email'] = 'Enter a valid email address.'
        if service not in SERVICE_LABELS:
            errors['service'] = 'Choose what you need.'
        if errors:
            return JsonResponse({'ok': False, 'errors': errors}, status=400)

        interest = SERVICE_LABELS[service]

        from apps.marketing.models import MarketingContact  # lazy import
        try:
            contact = MarketingContact.objects.create(
                full_name=full_name,
                company=company,
                email=email,
                phone=phone,
                stage=MarketingContact.Stage.LEAD,
                source=MarketingContact.Source.WEBSITE,
                interest=interest[:300],
                notes=f'Website enquiry ({ip})\n\n{message}' if message else f'Website enquiry ({ip})',
            )
        except Exception:
            logger.exception('Landing enquiry could not be saved')
            return JsonResponse(
                {'ok': False, 'error': 'Your request could not be sent. '
                                       'Try again, or email us directly.'},
                status=500,
            )

        cache.set(key, count + 1, THROTTLE_WINDOW)

        notify_to = getattr(settings, 'LANDING_ENQUIRY_EMAIL', '')
        if notify_to:
            from django.core.mail import send_mail  # lazy import
            body = (
                f'Name: {full_name}\nOrganisation: {company or "-"}\n'
                f'Email: {email}\nPhone: {phone or "-"}\nInterest: {interest}\n\n'
                f'{message or "(no message)"}\n\nContact ID: {contact.pk}'
            )
            try:
                send_mail(f'Website enquiry: {interest} — {full_name}', body,
                          settings.DEFAULT_FROM_EMAIL, [notify_to], fail_silently=True)
            except Exception:
                logger.exception('Landing enquiry email failed')

        return JsonResponse({'ok': True})
