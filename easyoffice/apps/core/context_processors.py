from django.conf import settings


def office_settings(request):
    return {'OFFICE_CONFIG': settings.OFFICE_CONFIG}


def unread_notifications(request):
    if request.user.is_authenticated:
        count = request.user.core_notifications.filter(is_read=False).count()
        return {'unread_notification_count': count}
    return {'unread_notification_count': 0}
