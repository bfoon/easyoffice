"""
InvestorConfinementMiddleware — external investors (InvestorAccount with
portal_only=True) may only reach the investor portal plus the auth pages they
need to sign in/out. Mirrors POIConfinementMiddleware and fails CLOSED.

Staff who also invest have portal_only=False and are not confined.
"""
from django.shortcuts import redirect


class InvestorConfinementMiddleware:
    ALLOWED_PREFIXES = (
        '/invest/',
        '/static/',
        '/media/',
        '/auth/login/',
        '/auth/logout/',
        '/auth/otp/',
        '/auth/switch/',
        '/users/change-password/',
        '/users/reset/',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated and not user.is_superuser:
            account = getattr(user, '_investor_account_cache', None)
            if account is None:
                try:
                    account = user.investor_account
                except Exception:
                    account = False
                user._investor_account_cache = account
            if account and account.portal_only:
                if not (request.path or '').startswith(self.ALLOWED_PREFIXES):
                    return redirect('invest:dashboard')
        return self.get_response(request)
