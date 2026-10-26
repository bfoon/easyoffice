def investment_flags(request):
    """Sidebar flag + pending-work badge for investment managers."""
    user = getattr(request, 'user', None)
    if not user or not getattr(user, 'is_authenticated', False):
        return {'is_investment_manager': False, 'investment_pending_count': 0}
    try:
        from apps.investments.permissions import is_investment_manager
        if not is_investment_manager(user):
            return {'is_investment_manager': False, 'investment_pending_count': 0}
        from apps.investments.models import (DepositRequest, Distribution,
                                             InvestorApplication, WithdrawalRequest)
        n = (InvestorApplication.objects.filter(status='submitted').count()
             + DepositRequest.objects.filter(status='pending').count()
             + WithdrawalRequest.objects.filter(status='pending').count()
             + Distribution.objects.filter(status='draft').count())
        return {'is_investment_manager': True, 'investment_pending_count': n}
    except Exception:
        return {'is_investment_manager': False, 'investment_pending_count': 0}
