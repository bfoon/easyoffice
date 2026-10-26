"""
apps/investments/services/ledger.py
───────────────────────────────────
Every money movement goes through here. Each public function opens its own
atomic block and locks the InvestorAccount row (select_for_update) before
touching balances, so two browser tabs can't spend the same money twice.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from apps.investments import conf

ZERO = Decimal('0.00')
CENT = Decimal('0.01')


class LedgerError(Exception):
    """User-facing money error — the message is safe to show."""


def money(value) -> Decimal:
    try:
        d = Decimal(str(value).replace(',', '').strip())
    except Exception:
        raise LedgerError('Enter a valid amount.')
    if not d.is_finite():
        raise LedgerError('Enter a valid amount.')
    return d.quantize(CENT, rounding=ROUND_HALF_UP)


def _locked_account(account_id):
    from apps.investments.models import InvestorAccount
    return InvestorAccount.objects.select_for_update().get(pk=account_id)


def _post(account, kind, amount: Decimal, *, description='', reference='',
          holding=None, actor=None, allow_negative=False):
    """Append a wallet line and update the cached balance. Caller holds the lock."""
    from apps.investments.models import WalletTransaction
    new_balance = (account.wallet_balance + amount).quantize(CENT)
    if new_balance < ZERO and not allow_negative:
        raise LedgerError(
            f'Not enough money in your wallet. Available: {conf.currency()} {account.wallet_balance:,.2f}.')
    account.wallet_balance = new_balance
    account.save(update_fields=['wallet_balance', 'updated_at'])
    return WalletTransaction.objects.create(
        account=account, kind=kind, amount=amount, balance_after=new_balance,
        holding=holding, reference=reference[:80], description=description[:300],
        created_by=actor if getattr(actor, 'pk', None) else None,
    )


def _require_active(account):
    if account.status != account.Status.ACTIVE:
        raise LedgerError('This investor account is not active.')


# ─────────────────────────────────────────────────────────────────────────────
# Deposits
# ─────────────────────────────────────────────────────────────────────────────

def create_deposit_request(account, *, amount, method, reference='', proof=None, note=''):
    from apps.investments.models import DepositRequest, PayoutMethod
    _require_active(account)
    amount = money(amount)
    if amount < conf.min_deposit():
        raise LedgerError(f'The minimum deposit is {conf.currency()} {conf.min_deposit():,.2f}.')
    if method not in PayoutMethod.values:
        raise LedgerError('Choose how you sent the money.')
    if method != PayoutMethod.CASH and not reference.strip() and not proof:
        raise LedgerError('Add the transaction ID or upload a receipt so we can match your payment.')
    dep = DepositRequest(account=account, amount=amount, method=method,
                         reference=reference.strip()[:80], note=note.strip()[:300])
    if proof:
        dep.proof = proof
    dep.save()
    return dep


def confirm_deposit(deposit, actor, note=''):
    from apps.investments.models import DepositRequest, WalletTransaction
    with transaction.atomic():
        dep = DepositRequest.objects.select_for_update().get(pk=deposit.pk)
        if dep.status != DepositRequest.Status.PENDING:
            raise LedgerError('This deposit has already been reviewed.')
        account = _locked_account(dep.account_id)
        txn = _post(account, WalletTransaction.Kind.DEPOSIT, dep.amount,
                    description=f'Deposit via {dep.get_method_display()}',
                    reference=dep.reference, actor=actor)
        dep.status = DepositRequest.Status.CONFIRMED
        dep.reviewed_by, dep.reviewed_at = actor, timezone.now()
        dep.review_note = note[:300]
        dep.wallet_txn = txn
        dep.save()
    return dep


def reject_deposit(deposit, actor, note=''):
    from apps.investments.models import DepositRequest
    with transaction.atomic():
        dep = DepositRequest.objects.select_for_update().get(pk=deposit.pk)
        if dep.status != DepositRequest.Status.PENDING:
            raise LedgerError('This deposit has already been reviewed.')
        dep.status = DepositRequest.Status.REJECTED
        dep.reviewed_by, dep.reviewed_at = actor, timezone.now()
        dep.review_note = (note or 'Payment could not be matched.')[:300]
        dep.save()
    return dep


# ─────────────────────────────────────────────────────────────────────────────
# Withdrawals — the amount is held (debited) at request time and returned
# if the request is rejected or cancelled.
# ─────────────────────────────────────────────────────────────────────────────

def request_withdrawal(account, *, amount, method, account_name='', account_number='',
                       bank_name='', note='', actor=None):
    from apps.investments.models import PayoutMethod, WalletTransaction, WithdrawalRequest
    amount = money(amount)
    if amount < conf.min_withdrawal():
        raise LedgerError(f'The minimum withdrawal is {conf.currency()} {conf.min_withdrawal():,.2f}.')
    if method not in PayoutMethod.values:
        raise LedgerError('Choose where we should send the money.')
    if method != PayoutMethod.CASH and not account_number.strip():
        raise LedgerError('Enter the account or phone number to pay into.')
    with transaction.atomic():
        acc = _locked_account(account.pk)
        _require_active(acc)
        wr = WithdrawalRequest.objects.create(
            account=acc, amount=amount, method=method,
            account_name=account_name.strip()[:180], account_number=account_number.strip()[:60],
            bank_name=bank_name.strip()[:120], note=note.strip()[:300],
        )
        wr.hold_txn = _post(acc, WalletTransaction.Kind.WITHDRAWAL, -amount,
                            description=f'Withdrawal to {wr.get_method_display()} (processing)',
                            reference=str(wr.pk)[:8], actor=actor)
        wr.save(update_fields=['hold_txn'])
    return wr


def _return_withdrawal(wr, actor, status, note):
    from apps.investments.models import WalletTransaction, WithdrawalRequest
    with transaction.atomic():
        w = WithdrawalRequest.objects.select_for_update().get(pk=wr.pk)
        if w.status != WithdrawalRequest.Status.PENDING:
            raise LedgerError('This withdrawal has already been processed.')
        acc = _locked_account(w.account_id)
        _post(acc, WalletTransaction.Kind.WITHDRAWAL_REVERSAL, w.amount,
              description='Withdrawal returned to wallet', reference=str(w.pk)[:8], actor=actor)
        w.status = status
        w.reviewed_by = actor if getattr(actor, 'pk', None) else None
        w.reviewed_at = timezone.now()
        w.review_note = note[:300]
        w.save()
    return w


def reject_withdrawal(wr, actor, note=''):
    from apps.investments.models import WithdrawalRequest
    return _return_withdrawal(wr, actor, WithdrawalRequest.Status.REJECTED, note or 'Rejected')


def cancel_withdrawal(wr, actor):
    from apps.investments.models import WithdrawalRequest
    return _return_withdrawal(wr, actor, WithdrawalRequest.Status.CANCELLED, 'Cancelled by investor')


def mark_withdrawal_paid(wr, actor, payout_reference='', note=''):
    from apps.investments.models import WithdrawalRequest
    with transaction.atomic():
        w = WithdrawalRequest.objects.select_for_update().get(pk=wr.pk)
        if w.status != WithdrawalRequest.Status.PENDING:
            raise LedgerError('This withdrawal has already been processed.')
        if w.method != 'cash' and not payout_reference.strip():
            raise LedgerError('Enter the payout transaction reference.')
        w.status = WithdrawalRequest.Status.PAID
        w.payout_reference = payout_reference.strip()[:80]
        w.reviewed_by, w.reviewed_at = actor, timezone.now()
        w.review_note = note[:300]
        w.save()
        if w.hold_txn_id:
            w.hold_txn.description = f'Withdrawal to {w.get_method_display()} — paid'
            w.hold_txn.reference = w.payout_reference or w.hold_txn.reference
            w.hold_txn.save(update_fields=['description', 'reference'])
    return w


def adjust_wallet(account, amount, note, actor):
    from apps.investments.models import WalletTransaction
    amount = money(amount)
    if amount == ZERO:
        raise LedgerError('Amount cannot be zero.')
    if not note.strip():
        raise LedgerError('A reason is required for manual adjustments.')
    with transaction.atomic():
        acc = _locked_account(account.pk)
        return _post(acc, WalletTransaction.Kind.ADJUSTMENT, amount,
                     description=note.strip(), actor=actor)


# ─────────────────────────────────────────────────────────────────────────────
# Investing
# ─────────────────────────────────────────────────────────────────────────────

def _event(holding, kind, amount, capital_delta, *, distribution=None, wallet_txn=None,
           note='', at=None):
    from apps.investments.models import HoldingEvent
    holding.capital = (holding.capital + capital_delta).quantize(CENT)
    return HoldingEvent.objects.create(
        holding=holding, kind=kind, amount=amount, capital_delta=capital_delta,
        capital_after=holding.capital, distribution=distribution, wallet_txn=wallet_txn,
        note=note[:300], occurred_at=at or timezone.now(),
    )


def invest(account, opportunity, amount, *, actor=None, payout_mode=None):
    from apps.investments.models import (Holding, HoldingEvent, InvestmentOpportunity,
                                         WalletTransaction)
    amount = money(amount)
    if amount <= ZERO:
        raise LedgerError('Enter an amount to invest.')

    with transaction.atomic():
        opp = InvestmentOpportunity.objects.select_for_update().get(pk=opportunity.pk)
        if not opp.is_investable:
            raise LedgerError('This opportunity is not accepting new investment right now.')
        acc = _locked_account(account.pk)
        _require_active(acc)

        holding, _ = Holding.objects.select_for_update().get_or_create(
            account=acc, opportunity=opp,
            defaults={'payout_mode': payout_mode or Holding.PayoutMode.REINVEST})

        if holding.capital + amount < opp.min_investment and holding.capital == ZERO:
            raise LedgerError(f'The minimum investment here is {conf.currency()} {opp.min_investment:,.2f}.')
        if opp.max_per_investor and holding.capital + amount > opp.max_per_investor:
            room = max(ZERO, opp.max_per_investor - holding.capital)
            raise LedgerError(f'The limit per investor is {conf.currency()} {opp.max_per_investor:,.2f}. '
                              f'You can add up to {conf.currency()} {room:,.2f}.')
        room = opp.remaining_capacity()
        if room is not None and amount > room:
            raise LedgerError(f'Only {conf.currency()} {room:,.2f} of this opportunity is still open.')

        txn = _post(acc, WalletTransaction.Kind.INVEST, -amount,
                    description=f'Invested in {opp.title}', holding=holding, actor=actor)
        now = timezone.now()
        holding.status = Holding.Status.ACTIVE
        holding.principal_contributed += amount
        holding.first_invested_at = holding.first_invested_at or now
        holding.last_invested_at = now
        if payout_mode in Holding.PayoutMode.values:
            holding.payout_mode = payout_mode
        _event(holding, HoldingEvent.Kind.INVEST, amount, amount, wallet_txn=txn,
               note='Investment', at=now)
        holding.save()
    return holding


def redeem(holding, amount, *, actor=None):
    """Move capital from a holding back to the wallet (early-exit fee inside lock-in)."""
    from apps.investments.models import Holding, HoldingEvent, WalletTransaction
    amount = money(amount)
    if amount <= ZERO:
        raise LedgerError('Enter an amount to withdraw from this investment.')

    with transaction.atomic():
        h = Holding.objects.select_for_update().select_related('opportunity').get(pk=holding.pk)
        acc = _locked_account(h.account_id)
        _require_active(acc)
        if amount > h.capital:
            raise LedgerError(f'You have {conf.currency()} {h.capital:,.2f} in this investment.')
        if h.opportunity.status == h.opportunity.Status.DRAFT:
            raise LedgerError('This opportunity is not live.')

        fee = ZERO
        if h.is_locked and h.opportunity.early_exit_fee_pct:
            fee = (amount * h.opportunity.early_exit_fee_pct / 100).quantize(CENT)

        credit = _post(acc, WalletTransaction.Kind.REDEEM, amount,
                       description=f'Capital returned from {h.opportunity.title}', holding=h, actor=actor)
        _event(h, HoldingEvent.Kind.REDEEM, amount, -amount, wallet_txn=credit,
               note='Capital withdrawn to wallet')
        h.redeemed_total += amount
        if fee > ZERO:
            fee_txn = _post(acc, WalletTransaction.Kind.FEE, -fee,
                            description=f'Early-exit fee ({h.opportunity.early_exit_fee_pct}%)',
                            holding=h, actor=actor)
            _event(h, HoldingEvent.Kind.FEE, fee, ZERO, wallet_txn=fee_txn,
                   note='Withdrawn before the lock-in ended')
            h.fees_total += fee
        if h.capital == ZERO:
            h.status = Holding.Status.CLOSED
        h.save()
    return h, fee


def set_payout_mode(holding, mode):
    from apps.investments.models import Holding
    if mode not in Holding.PayoutMode.values:
        raise LedgerError('Choose how you want to receive profit.')
    Holding.objects.filter(pk=holding.pk).update(payout_mode=mode, updated_at=timezone.now())
    holding.payout_mode = mode
    return holding


def reconcile(account) -> dict:
    """Recompute the wallet balance from the ledger (diagnostics)."""
    from django.db.models import Sum
    ledger = account.wallet_transactions.aggregate(s=Sum('amount'))['s'] or ZERO
    return {'cached': account.wallet_balance, 'ledger': ledger,
            'ok': ledger.quantize(CENT) == account.wallet_balance.quantize(CENT)}
