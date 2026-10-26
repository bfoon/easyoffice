from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from apps.investments import conf


def is_investment_manager(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if user.is_superuser:
        return True
    return user.groups.filter(name__in=conf.manager_groups()).exists()


def investor_account_for(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    try:
        return user.investor_account
    except Exception:
        return None


class InvestmentManagerMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not is_investment_manager(request.user):
            raise PermissionDenied('Investment managers only.')
        return super().dispatch(request, *args, **kwargs)


class InvestorRequiredMixin(LoginRequiredMixin):
    """Active investor accounts only; sets self.account."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        account = investor_account_for(request.user)
        if account is None:
            if is_investment_manager(request.user):
                return redirect('investments:dashboard')
            raise PermissionDenied('No investor account.')
        if account.status != account.Status.ACTIVE:
            from django.shortcuts import render
            return render(request, 'investments/portal/inactive.html',
                          {'account': account}, status=403)
        self.account = account
        return super().dispatch(request, *args, **kwargs)
