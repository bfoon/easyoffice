def unread_messages(request):
    if request.user.is_authenticated:
        from apps.messaging.models import ChatRoomMember, ChatMessage
        from django.db.models import Q
        import django.utils.timezone as tz
        try:
            member_rooms = ChatRoomMember.objects.filter(user=request.user).select_related('room')
            count = 0
            for m in member_rooms:
                last_read = m.last_read
                if last_read:
                    count += m.room.messages.filter(
                        created_at__gt=last_read, is_deleted=False
                    ).exclude(sender=request.user).count()
                else:
                    count += m.room.messages.filter(
                        is_deleted=False
                    ).exclude(sender=request.user).count()
            return {'unread_message_count': count}
        except Exception:
            pass
    return {'unread_message_count': 0}
