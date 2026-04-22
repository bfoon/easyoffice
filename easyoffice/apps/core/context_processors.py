from datetime import timedelta
from django.conf import settings
from django.urls import reverse
from django.utils import timezone


def office_settings(request):
    """
    Global office branding/settings for templates.
    """
    return {
        'OFFICE_CONFIG': getattr(settings, 'OFFICE_CONFIG', {
            'NAME': 'EasyOffice',
            'TAGLINE': 'Your Virtual Office Management Platform',
            'LOGO_URL': '',
            'PRIMARY_COLOR': '#1e3a5f',
            'ACCENT_COLOR': '#2196f3',
            'CURRENCY': 'USD',
            'CURRENCY_SYMBOL': '$',
        })
    }


def _ensure_due_task_alerts(user):
    if not user.is_authenticated:
        return

    from apps.core.models import CoreNotification
    from apps.tasks.models import Task

    now = timezone.now()
    soon_cutoff = now + timedelta(hours=24)

    active_tasks = Task.objects.filter(
        assigned_to=user,
        status__in=['todo', 'in_progress', 'review'],
        due_date__isnull=False,
        due_date__lte=soon_cutoff,
    ).select_related('assigned_by', 'project')

    for task in active_tasks:
        task_url = reverse('task_detail', kwargs={'pk': task.pk})

        if task.is_overdue:
            phase = 'overdue'
            title = f'Overdue Task: {task.title}'
            message = f'"{task.title}" is overdue. Please review it now.'
            actions = [
                {
                    'label': 'Open Task',
                    'url': task_url,
                    'style': 'primary',
                    'icon': 'bi-box-arrow-up-right',
                }
            ]
        else:
            phase = 'due_soon'
            hours_left = max(1, int((task.due_date - now).total_seconds() // 3600))
            title = f'Task Due Soon: {task.title}'
            message = f'"{task.title}" is due in about {hours_left} hour(s).'
            actions = [
                {
                    'label': 'Open Task',
                    'url': task_url,
                    'style': 'primary',
                    'icon': 'bi-box-arrow-up-right',
                }
            ]

        alert_key = f'{phase}:{task.pk}:{task.due_date.isoformat()}'

        already_exists = CoreNotification.objects.filter(
            recipient=user,
            data__alert_key=alert_key,
        ).exists()

        if already_exists:
            continue

        CoreNotification.objects.create(
            recipient=user,
            sender=task.assigned_by,
            notification_type='task',
            title=title,
            message=message,
            link=task_url,
            data={
                'alert_key': alert_key,
                'task_id': str(task.pk),
                'actions': actions,
            },
        )


def unread_notifications(request):
    """
    Bell notification data for base.html
    """
    if request.user.is_authenticated:
        try:
            from apps.core.models import CoreNotification

            _ensure_due_task_alerts(request.user)

            unread_qs = request.user.core_notifications.filter(
                is_read=False
            ).order_by('-created_at')

            return {
                'unread_notification_count': unread_qs.count(),
                'unread_notifications': unread_qs[:20],
            }
        except Exception:
            pass

    return {
        'unread_notification_count': 0,
        'unread_notifications': [],
    }


def unread_messages(request):
    """
    Unread chat count for sidebar/topbar badges.
    """
    if request.user.is_authenticated:
        try:
            from apps.messaging.models import ChatRoomMember

            member_rooms = ChatRoomMember.objects.filter(
                user=request.user
            ).select_related('room')

            unread_message_count = 0
            for member in member_rooms:
                last_read = member.last_read
                if last_read:
                    unread_message_count += member.room.messages.filter(
                        created_at__gt=last_read,
                        is_deleted=False
                    ).exclude(sender=request.user).count()
                else:
                    unread_message_count += member.room.messages.filter(
                        is_deleted=False
                    ).exclude(sender=request.user).count()

            return {
                'unread_message_count': unread_message_count,
            }
        except Exception:
            pass

    return {
        'unread_message_count': 0,
    }