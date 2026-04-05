from django.utils import timezone


class ActiveUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            request.user.update_last_seen()
        return self.get_response(request)


class OfficeBrandingMiddleware:
    """Inject office branding into every request."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings
        request.office_config = settings.OFFICE_CONFIG
        return self.get_response(request)
