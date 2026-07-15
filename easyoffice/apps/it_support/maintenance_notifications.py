"""Email and in-app notifications for equipment maintenance."""
from __future__ import annotations

import logging
from email.mime.application import MIMEApplication

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.urls import reverse

from .models import MaintenanceEvent, MaintenanceNotificationLog

logger = logging.getLogger(__name__)


def _tracking_url(job):
    path = reverse('maintenance_public', kwargs={'token': job.tracking_token.token})
    base = getattr(settings, 'SITE_URL', '').rstrip('/')
    return f'{base}{path}' if base else path


def _log(job, event_type, channel, recipient, subject, status, error=''):
    MaintenanceNotificationLog.objects.create(
        job=job,
        event_type=event_type,
        channel=channel,
        recipient=recipient or '',
        subject=subject or '',
        status=status,
        error=error,
    )


def _document_attachment(document):
    if not document or not document.generated_pdf_id:
        return None
    try:
        document.generated_pdf.file.open('rb')
        data = document.generated_pdf.file.read()
        document.generated_pdf.file.close()
        name = f'{document.number or document.pk}.pdf'
        return name, data, 'application/pdf'
    except Exception:
        logger.exception('Could not read generated PDF for document %s', document.pk)
        return None


def _send_email(job, event_type, subject, body, *, attachments=None):
    recipient = job.effective_owner_email
    if not recipient:
        _log(
            job, event_type, 'email', '', subject,
            MaintenanceNotificationLog.Status.SKIPPED,
            'Owner has no email address; document remains available for printing.'
        )
        return False

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
            to=[recipient],
        )
        for attachment in attachments or []:
            if attachment:
                msg.attach(*attachment)
        msg.send(fail_silently=False)
        _log(job, event_type, 'email', recipient, subject, MaintenanceNotificationLog.Status.SENT)
        return True
    except Exception as exc:
        logger.exception('Maintenance email failed for %s', job.maintenance_number)
        _log(
            job, event_type, 'email', recipient, subject,
            MaintenanceNotificationLog.Status.FAILED, str(exc)
        )
        return False


def _notify_users(job, users, title, message, link):
    try:
        from apps.core.models import CoreNotification
        for user in users:
            CoreNotification.objects.create(
                recipient=user,
                sender=job.created_by,
                notification_type='it',
                title=title,
                message=message,
                link=link,
            )
            _log(
                job, 'in_app', 'in_app', getattr(user, 'email', ''), title,
                MaintenanceNotificationLog.Status.SENT
            )
    except Exception as exc:
        logger.exception('In-app maintenance notification failed')
        _log(
            job, 'in_app', 'in_app', '', title,
            MaintenanceNotificationLog.Status.FAILED, str(exc)
        )


def _it_users():
    from apps.core.models import User
    return User.objects.filter(groups__name='IT', is_active=True).distinct()


def send_tracking_link(job, tracking_url, actor=None):
    subject = f'{job.maintenance_number}: track your equipment maintenance'
    body = (
        f'Dear {job.owner_display_name},\n\n'
        f'Your {job.equipment_name} has been registered for maintenance.\n'
        f'Reference: {job.maintenance_number}\n\n'
        f'Track progress securely using this link:\n{tracking_url}\n\n'
        'The link will stop working automatically after the maintenance record is closed.\n\n'
        'Thank you.'
    )
    return _send_email(job, 'tracking_link', subject, body)


def notify_job_created(job, tracking_url, actor=None):
    send_tracking_link(job, tracking_url, actor=actor)
    _notify_users(
        job, _it_users(),
        f'New maintenance: {job.maintenance_number}',
        f'{job.equipment_name} received from {job.owner_display_name}.',
        reverse('maintenance_detail', kwargs={'pk': job.pk}),
    )
    if job.assigned_to_id:
        notify_assignment(job, actor=actor)


def notify_assignment(job, actor=None):
    if not job.assigned_to_id:
        return
    _notify_users(
        job, [job.assigned_to],
        f'Assigned maintenance: {job.maintenance_number}',
        f'{job.equipment_name} has been assigned to you.',
        reverse('maintenance_detail', kwargs={'pk': job.pk}),
    )


def notify_status_changed(job, actor=None):
    subject = f'{job.maintenance_number}: {job.get_status_display()}'
    body = (
        f'Dear {job.owner_display_name},\n\n'
        f'The status of your {job.equipment_name} is now: {job.get_status_display()}.\n'
        f'{job.owner_visible_note}\n\n'
        f'Reference: {job.maintenance_number}\n\n'
        f'Track progress: {_tracking_url(job)}\n'
    )
    _send_email(job, 'status_changed', subject, body)


def notify_ready_for_pickup(job, actor=None):
    subject = f'{job.maintenance_number}: your equipment is ready for pickup'
    deadline = job.pickup_deadline_at
    body = (
        f'Dear {job.owner_display_name},\n\n'
        f'Your {job.equipment_name} is ready for pickup.\n'
        f'Amount due: {job.balance_due} {job.currency}\n'
        f'Pickup deadline: {deadline:%d %b %Y %H:%M}\n\n'
        f'Reference: {job.maintenance_number}\n'
        f'Track or confirm pickup: {_tracking_url(job)}\n'
    )
    attachments = [_document_attachment(job.invoice)]
    _send_email(job, 'ready_pickup', subject, body, attachments=attachments)


def notify_receipt_ready(job, actor=None):
    subject = f'{job.maintenance_number}: payment receipt'
    body = (
        f'Dear {job.owner_display_name},\n\n'
        f'Payment for {job.equipment_name} has been recorded. '
        f'Your receipt is attached.\n\nThank you.'
    )
    _send_email(
        job, 'receipt_ready', subject, body,
        attachments=[_document_attachment(job.receipt)]
    )


def notify_job_closed(job, actor=None):
    subject = f'{job.maintenance_number}: maintenance completed and closed'
    body = (
        f'Dear {job.owner_display_name},\n\n'
        f'The maintenance record for {job.equipment_name} is now closed.\n'
        f'Warranty valid until: {job.warranty_until or "Not applicable"}\n\n'
        'The tracking link has now expired. Your invoice and receipt are attached.\n\n'
        'Thank you.'
    )
    attachments = [
        _document_attachment(job.invoice),
        _document_attachment(job.receipt),
    ]
    _send_email(job, 'job_closed', subject, body, attachments=attachments)


def send_reminder(reminder):
    job = reminder.job
    link = reverse('maintenance_detail', kwargs={'pk': job.pk})
    target_users = []

    if reminder.target == reminder.Target.OWNER:
        return _send_email(job, f'reminder:{reminder.key}', reminder.subject, reminder.message)
    if reminder.target == reminder.Target.TECHNICIAN and job.assigned_to_id:
        target_users = [job.assigned_to]
    elif reminder.target == reminder.Target.IT:
        target_users = list(_it_users())

    if reminder.channel in (reminder.Channel.IN_APP, reminder.Channel.BOTH):
        _notify_users(job, target_users, reminder.subject, reminder.message, link)
    if reminder.channel in (reminder.Channel.EMAIL, reminder.Channel.BOTH):
        for user in target_users:
            recipient = getattr(user, 'email', '')
            if not recipient:
                continue
            try:
                EmailMultiAlternatives(
                    subject=reminder.subject,
                    body=reminder.message,
                    from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
                    to=[recipient],
                ).send(fail_silently=False)
            except Exception:
                logger.exception('Reminder email failed for %s', recipient)
    return True
