"""
apps/investments/services/notify.py
───────────────────────────────────
Email to investors and bell notifications to investment managers.
Everything fails soft — a mail outage never blocks a money operation.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse

from apps.investments import conf

logger = logging.getLogger(__name__)


def _brand():
    return getattr(settings, 'OFFICE_CONFIG', {}).get('NAME', 'EasyOffice')


def send_email(to, subject, heading, paragraphs, button=None, footer=''):
    """Generic branded email. button = (label, absolute_url)."""
    if not to:
        return False
    ctx = {'brand': _brand(), 'heading': heading, 'paragraphs': paragraphs,
           'button': button, 'footer': footer, 'currency': conf.currency()}
    text = '\n\n'.join([heading] + list(paragraphs) +
                       ([f'{button[0]}: {button[1]}'] if button else []) +
                       ([footer] if footer else []))
    try:
        html = render_to_string('investments/email/message.html', ctx)
        msg = EmailMultiAlternatives(f'{_brand()} Invest — {subject}', text,
                                     settings.DEFAULT_FROM_EMAIL, [to])
        msg.attach_alternative(html, 'text/html')
        msg.send(fail_silently=False)
        return True
    except Exception:
        logger.exception('investments: email to %s failed (%s)', to, subject)
        return False


def _abs(path):
    return conf.site_url() + path


def _managers():
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    User = get_user_model()
    return User.objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(groups__name__in=conf.manager_groups())).distinct()


def notify_managers(title, message, link):
    try:
        from apps.core.models import CoreNotification
        for u in _managers():
            CoreNotification.objects.create(
                recipient=u, notification_type='finance', title=title[:255],
                message=message, link=link,
                data={'actions': [{'label': 'Open', 'url': link, 'style': 'primary',
                                   'icon': 'bi-box-arrow-up-right'}]},
            )
    except Exception:
        logger.exception('investments: manager notification failed')


# ─────────────────────────────────────────────────────────────────────────────
# Investor-facing
# ─────────────────────────────────────────────────────────────────────────────

def invitation(inv):
    paras = [
        f'Hello{" " + inv.full_name if inv.full_name else ""},',
        f'You are invited to invest with {_brand()}. Investors earn a share of the profit '
        f'made when the items in a listing are sold, and can watch their capital and profit grow '
        f'on a personal dashboard.',
    ]
    if inv.personal_message:
        paras.append(inv.personal_message)
    paras.append(f'The application takes about 10 minutes. Have your ID ready. '
                 f'This link expires on {inv.expires_at:%d %B %Y}.')
    return send_email(inv.email, 'Your invitation to invest', 'You are invited to invest',
                      paras, ('Start your application', inv.public_url))


def application_received(app):
    send_email(app.email, 'Application received', 'We have your application',
               [f'Thank you, {app.full_name}.',
                'Our team will review your details and documents, usually within two working days. '
                'We will email you as soon as there is a decision.'])
    notify_managers('New investor application',
                    f'{app.full_name} ({app.email}) applied to invest.',
                    reverse('investments:application_detail', args=[app.pk]))


def application_info_requested(app):
    url = app.invitation.public_url if app.invitation else conf.site_url()
    send_email(app.email, 'We need a little more information', 'One more step',
               [f'Hello {app.full_name},',
                'Before we can approve your application we need the following:',
                app.info_request_note or '',
                'Use the button below to update your application.'],
               ('Update my application', url))


def application_rejected(app):
    send_email(app.email, 'About your application', 'Application update',
               [f'Hello {app.full_name},',
                'Thank you for your interest. We are not able to open an investor account for you at this time.',
                app.review_note or ''])


def account_approved(account, existing_user=False):
    if existing_user:
        url = _abs(reverse('invest:dashboard'))
        send_email(account.user.email, 'Your investor account is ready', 'Welcome aboard',
                   ['Your application has been approved. Sign in with your existing account '
                    'to open the investor portal.', f'Account number: {account.account_no}'],
                   ('Open the investor portal', url))
        return
    url = _abs(reverse('invest:activate', args=[account.activation_token]))
    send_email(account.user.email, 'Approved — set your password', 'Your application is approved',
               [f'Welcome, {account.display_name}.',
                f'Your investor account number is {account.account_no}.',
                f'Set your password to sign in. This link works for {conf.activation_hours()} hours.'],
               ('Set my password', url))


def deposit_submitted(dep):
    notify_managers('Deposit to confirm',
                    f'{dep.account.display_name}: {conf.currency()} {dep.amount:,.2f} via {dep.get_method_display()}',
                    reverse('investments:deposit_list'))


def deposit_reviewed(dep):
    ok = dep.status == dep.Status.CONFIRMED
    send_email(dep.account.user.email,
               'Deposit confirmed' if ok else 'Deposit not confirmed',
               'Money added to your wallet' if ok else 'We could not confirm your deposit',
               [f'{conf.currency()} {dep.amount:,.2f} via {dep.get_method_display()}.',
                (f'Your wallet balance is now {conf.currency()} {dep.account.wallet_balance:,.2f}. '
                 'You can invest it in any open listing.') if ok else (dep.review_note or '')],
               ('Open my wallet', _abs(reverse('invest:wallet'))))


def withdrawal_submitted(wr):
    notify_managers('Withdrawal to pay',
                    f'{wr.account.display_name}: {conf.currency()} {wr.amount:,.2f} to {wr.get_method_display()}',
                    reverse('investments:withdrawal_list'))


def withdrawal_reviewed(wr):
    paid = wr.status == wr.Status.PAID
    send_email(wr.account.user.email,
               'Withdrawal paid' if paid else 'Withdrawal returned to your wallet',
               'Your money is on its way' if paid else 'Withdrawal not processed',
               [f'{conf.currency()} {wr.amount:,.2f} to {wr.get_method_display()} {wr.account_number}.',
                f'Reference: {wr.payout_reference}' if paid else
                (wr.review_note or '') + ' The amount is back in your wallet.'],
               ('Open my wallet', _abs(reverse('invest:wallet'))))


def distribution_posted(dist):
    from datetime import timedelta
    from django.utils import timezone
    period = f'{timezone.localtime(dist.period_start):%d %b} – ' \
             f'{timezone.localtime(dist.period_end - timedelta(seconds=1)):%d %b %Y}'
    for alloc in dist.allocations.select_related('holding__account__user'):
        if alloc.amount == 0:
            continue
        h = alloc.holding
        if alloc.amount > 0:
            where = ('added to your investment, so it keeps growing'
                     if alloc.mode_applied == 'reinvest' else 'paid into your wallet')
            paras = [f'{dist.opportunity.title} made a profit for {period}.',
                     f'Your share is {conf.currency()} {alloc.amount:,.2f}, {where}.',
                     f'Your capital in this listing is now {conf.currency()} {h.capital:,.2f}.']
            subject = f'You earned {conf.currency()} {alloc.amount:,.2f}'
        else:
            paras = [f'{dist.opportunity.title} made a loss for {period}.',
                     f'Your share of the loss is {conf.currency()} {-alloc.amount:,.2f}.',
                     f'Your capital in this listing is now {conf.currency()} {h.capital:,.2f}.']
            subject = 'Period result: loss'
        send_email(h.account.user.email, subject, 'Profit update', paras,
                   ('See my dashboard', _abs(reverse('invest:holding_detail', args=[h.pk]))))


def drafts_waiting(count):
    notify_managers('Profit distributions to approve',
                    f'{count} distribution{"s" if count != 1 else ""} are waiting for approval.',
                    reverse('investments:distribution_list') + '?status=draft')


def opportunity_update_posted(update):
    from apps.investments.models import Holding
    holders = (Holding.objects.filter(opportunity=update.opportunity, status=Holding.Status.ACTIVE)
               .select_related('account__user'))
    url = _abs(reverse('invest:opportunity_detail', args=[update.opportunity.slug]))
    for h in holders:
        send_email(h.account.user.email, f'Update: {update.opportunity.title}', update.title,
                   [update.body], ('Read on the portal', url))
