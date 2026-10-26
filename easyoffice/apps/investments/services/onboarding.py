"""
apps/investments/services/onboarding.py
───────────────────────────────────────
Invitation → application (KYC) → review → account → activation.
"""
from __future__ import annotations

import re
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from apps.investments import conf

MAX_UPLOAD = 10 * 1024 * 1024
ALLOWED_EXT = ('.pdf', '.jpg', '.jpeg', '.png')
_MAGIC = (b'%PDF', b'\x89PNG', b'\xff\xd8\xff')


class OnboardingError(Exception):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Invitations
# ─────────────────────────────────────────────────────────────────────────────

def parse_recipients(text: str):
    """Accept 'email', 'Name <email>' or 'Name, email' per line or comma-separated."""
    out, bad = [], []
    for raw in re.split(r'[\n;]+', text or ''):
        raw = raw.strip()
        if not raw:
            continue
        m = re.match(r'^(.*?)<([^>]+)>$', raw)
        if m:
            name, email = m.group(1).strip(' ,"'), m.group(2).strip()
            parts = [(name, email)]
        else:
            tokens = [t.strip() for t in raw.split(',') if t.strip()]
            emails = [t for t in tokens if '@' in t]
            names = [t for t in tokens if '@' not in t]
            if len(emails) == 1 and len(names) == 1:
                parts = [(names[0], emails[0])]
            else:
                parts = [('', e) for e in emails]
                bad += names if len(emails) != 1 else []
        for name, email in parts:
            try:
                validate_email(email)
                out.append((name, email.lower()))
            except ValidationError:
                bad.append(email)
    seen, uniq = set(), []
    for name, email in out:
        if email not in seen:
            seen.add(email)
            uniq.append((name, email))
    return uniq, bad


def create_invitations(recipients, *, message='', actor=None, send=True):
    from apps.investments.models import InvestorAccount, InvestorInvitation
    from apps.investments.services import notify
    created, skipped = [], []
    for name, email in recipients:
        if InvestorAccount.objects.filter(user__email__iexact=email).exists():
            skipped.append((email, 'already an investor'))
            continue
        live = InvestorInvitation.objects.filter(
            email__iexact=email,
            status__in=[InvestorInvitation.Status.SENT, InvestorInvitation.Status.OPENED],
            expires_at__gt=timezone.now()).first()
        if live:
            skipped.append((email, 'invitation already active — use Resend'))
            continue
        inv = InvestorInvitation.objects.create(email=email, full_name=name,
                                                personal_message=message, invited_by=actor)
        if send:
            send_invitation(inv)
        created.append(inv)
    return created, skipped


def send_invitation(inv):
    from apps.investments.services import notify
    if inv.status == inv.Status.REVOKED:
        raise OnboardingError('This invitation was revoked.')
    if inv.is_expired:
        inv.expires_at = timezone.now() + timedelta(days=conf.invite_days())
    ok = notify.invitation(inv)
    inv.sent_count += 1
    inv.last_sent_at = timezone.now()
    inv.save()
    return ok


def get_invitation(token):
    from apps.investments.models import InvestorInvitation
    return InvestorInvitation.objects.filter(token=token).select_related('application').first()


def mark_opened(inv):
    if inv.status == inv.Status.SENT:
        inv.status = inv.Status.OPENED
        inv.opened_at = timezone.now()
        inv.save(update_fields=['status', 'opened_at', 'updated_at'])


# ─────────────────────────────────────────────────────────────────────────────
# Application form
# ─────────────────────────────────────────────────────────────────────────────

def _check_upload(f, label, required):
    if not f:
        if required:
            return f'Upload your {label}.'
        return None
    name = (f.name or '').lower()
    if not name.endswith(ALLOWED_EXT):
        return f'{label.capitalize()} must be a PDF, JPG or PNG.'
    if f.size > MAX_UPLOAD:
        return f'{label.capitalize()} is larger than 10 MB.'
    head = f.read(8)
    f.seek(0)
    if not any(head.startswith(m) for m in _MAGIC):
        return f'{label.capitalize()} does not look like a real PDF or image.'
    return None


def validate_application(post, files, existing=None):
    """Returns (cleaned_dict, errors_dict). Manual validation (no Django forms)."""
    from apps.investments.models import InvestorApplication as A, PayoutMethod
    g = lambda k, n=300: (post.get(k) or '').strip()[:n]
    errors, c = {}, {}

    for key, label, n in (('full_name', 'your full name', 180), ('phone', 'a phone number', 30),
                          ('id_number', 'your ID number', 60), ('address', 'your address', 300),
                          ('nationality', 'your nationality', 80)):
        c[key] = g(key, n)
        if not c[key]:
            errors[key] = f'Enter {label}.'
    c['email'] = g('email', 254).lower()
    try:
        validate_email(c['email'])
    except ValidationError:
        errors['email'] = 'Enter a valid email address.'

    for key, n in (('city', 80), ('country', 80), ('occupation', 120), ('employer', 160),
                   ('source_of_funds_detail', 300), ('next_of_kin_name', 180),
                   ('next_of_kin_phone', 30), ('next_of_kin_relationship', 60),
                   ('payout_account_name', 180), ('payout_account_number', 60),
                   ('payout_bank_name', 120), ('signature_name', 180)):
        c[key] = g(key, n)
    c['country'] = c['country'] or 'The Gambia'

    dob = parse_date(g('date_of_birth', 10)) if g('date_of_birth', 10) else None
    if not dob:
        errors['date_of_birth'] = 'Enter your date of birth.'
    else:
        age = (timezone.localdate() - dob).days / 365.25
        if age < 18:
            errors['date_of_birth'] = 'You must be 18 or older to invest.'
        elif age > 120:
            errors['date_of_birth'] = 'Check your date of birth.'
    c['date_of_birth'] = dob

    for key, choices, default in (('id_type', A.IdType, A.IdType.NATIONAL_ID),
                                  ('source_of_funds', A.Funds, A.Funds.SALARY),
                                  ('risk_tolerance', A.Risk, A.Risk.MEDIUM),
                                  ('horizon', A.Horizon, A.Horizon.MID),
                                  ('payout_method', PayoutMethod, PayoutMethod.WAVE)):
        v = g(key, 20)
        c[key] = v if v in choices.values else default

    try:
        c['expected_amount'] = Decimal((post.get('expected_amount') or '0').replace(',', '')).quantize(Decimal('0.01'))
        if c['expected_amount'] < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        errors['expected_amount'] = 'Enter an amount in numbers.'
        c['expected_amount'] = Decimal('0')

    if c['payout_method'] != PayoutMethod.CASH and not c['payout_account_number']:
        errors['payout_account_number'] = 'Enter the account or phone number for payouts.'

    has_doc = bool(existing and existing.id_document)
    err = _check_upload(files.get('id_document'), 'ID document', required=not has_doc)
    if err:
        errors['id_document'] = err
    err = _check_upload(files.get('proof_of_address'), 'proof of address', required=False)
    if err:
        errors['proof_of_address'] = err

    if post.get('agreed_terms') != '1':
        errors['agreed_terms'] = 'You need to accept the terms and risk statement.'
    if not c['signature_name']:
        errors['signature_name'] = 'Type your full name to sign.'
    elif c['signature_name'].lower() != c['full_name'].lower():
        errors['signature_name'] = 'Your signature must match your full name.'

    return c, errors


def submit_application(invitation, cleaned, files, ip=None):
    from apps.investments.models import InvestorApplication
    from apps.investments.services import notify
    with transaction.atomic():
        app = getattr(invitation, 'application', None) if invitation else None
        if app and app.status not in (InvestorApplication.Status.INFO_REQUESTED,):
            raise OnboardingError('This application has already been submitted.')
        app = app or InvestorApplication(invitation=invitation)
        for k, v in cleaned.items():
            setattr(app, k, v)
        if files.get('id_document'):
            app.id_document = files['id_document']
        if files.get('proof_of_address'):
            app.proof_of_address = files['proof_of_address']
        app.agreed_terms = True
        app.signed_at = timezone.now()
        app.signed_ip = ip
        app.status = InvestorApplication.Status.SUBMITTED
        app.save()
        if invitation:
            invitation.status = invitation.Status.SUBMITTED
            invitation.save(update_fields=['status', 'updated_at'])
    transaction.on_commit(lambda: notify.application_received(app))
    return app


# ─────────────────────────────────────────────────────────────────────────────
# Review
# ─────────────────────────────────────────────────────────────────────────────

def _unique_username(email):
    User = get_user_model()
    base = email.lower()[:140]
    name, n = base, 2
    while User.objects.filter(username=name).exists():
        name, n = f'{base}.{n}', n + 1
    return name


def approve_application(app, actor, note=''):
    from apps.investments.models import InvestorAccount, InvestorApplication
    from apps.investments.services import notify
    User = get_user_model()

    with transaction.atomic():
        app = InvestorApplication.objects.select_for_update().get(pk=app.pk)
        if app.status == InvestorApplication.Status.APPROVED:
            raise OnboardingError('Already approved.')
        if app.status == InvestorApplication.Status.REJECTED:
            raise OnboardingError('This application was rejected.')

        user = User.objects.filter(email__iexact=app.email).first()
        existing = user is not None
        if existing and InvestorAccount.objects.filter(user=user).exists():
            raise OnboardingError('That email already has an investor account.')
        if not existing:
            first, _, last = app.full_name.partition(' ')
            user = User(username=_unique_username(app.email), email=app.email.lower(),
                        first_name=first[:150], last_name=last[:150], phone=app.phone,
                        is_staff=False, is_active=True)
            user.set_unusable_password()
            user.save()

        account = InvestorAccount.objects.create(
            user=user, application=app, approved_by=actor,
            portal_only=not existing,
            status=InvestorAccount.Status.ACTIVE if existing else InvestorAccount.Status.PENDING_ACTIVATION,
            activated_at=timezone.now() if existing else None,
            risk_tolerance=app.risk_tolerance, payout_method=app.payout_method,
            payout_account_name=app.payout_account_name or app.full_name,
            payout_account_number=app.payout_account_number,
            payout_bank_name=app.payout_bank_name,
            activation_token='' if existing else secrets.token_urlsafe(32),
            activation_expires=None if existing else timezone.now() + timedelta(hours=conf.activation_hours()),
        )
        app.status = InvestorApplication.Status.APPROVED
        app.reviewed_by, app.reviewed_at, app.review_note = actor, timezone.now(), note
        app.save()
    transaction.on_commit(lambda: notify.account_approved(account, existing_user=existing))
    return account


def reject_application(app, actor, note=''):
    from apps.investments.services import notify
    if app.status == app.Status.APPROVED:
        raise OnboardingError('Approved applications cannot be rejected; suspend the account instead.')
    app.status = app.Status.REJECTED
    app.reviewed_by, app.reviewed_at, app.review_note = actor, timezone.now(), note
    app.save()
    notify.application_rejected(app)
    return app


def request_info(app, actor, note):
    from apps.investments.services import notify
    if not note.strip():
        raise OnboardingError('Say what information you need.')
    if app.status == app.Status.APPROVED:
        raise OnboardingError('Already approved.')
    app.status = app.Status.INFO_REQUESTED
    app.info_request_note = note.strip()
    app.reviewed_by, app.reviewed_at = actor, timezone.now()
    app.save()
    if app.invitation:
        inv = app.invitation
        if inv.is_expired:
            inv.expires_at = timezone.now() + timedelta(days=conf.invite_days())
        inv.status = inv.Status.OPENED
        inv.save()
    notify.application_info_requested(app)
    return app


def resend_activation(account):
    from apps.investments.services import notify
    if account.status != account.Status.PENDING_ACTIVATION:
        raise OnboardingError('This account is already activated.')
    account.activation_token = secrets.token_urlsafe(32)
    account.activation_expires = timezone.now() + timedelta(hours=conf.activation_hours())
    account.save(update_fields=['activation_token', 'activation_expires', 'updated_at'])
    notify.account_approved(account)


def account_for_activation(token):
    from apps.investments.models import InvestorAccount
    if not token:
        return None
    acc = InvestorAccount.objects.filter(activation_token=token).select_related('user').first()
    if not acc or acc.status != acc.Status.PENDING_ACTIVATION:
        return None
    if acc.activation_expires and acc.activation_expires < timezone.now():
        return None
    return acc


def activate(account, password, confirm):
    if password != confirm:
        raise OnboardingError('The two passwords do not match.')
    try:
        validate_password(password, account.user)
    except ValidationError as exc:
        raise OnboardingError(' '.join(exc.messages))
    with transaction.atomic():
        account.user.set_password(password)
        account.user.save()
        account.status = account.Status.ACTIVE
        account.activated_at = timezone.now()
        account.activation_token = ''
        account.activation_expires = None
        account.save()
    return account
