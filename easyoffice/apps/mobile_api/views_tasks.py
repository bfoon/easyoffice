"""
apps/mobile_api/views_tasks.py
──────────────────────────────
JWT-authenticated task endpoints for the mobile app. These wrap the SAME
model logic the web views use (task.mark_on_site / mark_done_pending_verify),
so mobile and web stay consistent — including customer-service verification.
"""

import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.tasks.models import Task
from apps.mobile_api.serializers_tasks import (
    TaskListSerializer, TaskDetailSerializer,
)

logger = logging.getLogger(__name__)

OPEN_STATUSES = ['todo', 'in_progress', 'review', 'on_hold']
CLOSED_STATUSES = ['done', 'cancelled']


def _ctx(request):
    return {'request': request, 'viewer': request.user}


class MyTasksView(APIView):
    """GET tasks/ — open tasks assigned to me."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (
            Task.objects
            .filter(assigned_to=request.user, status__in=OPEN_STATUSES)
            .select_related('assigned_by', 'category')
            .order_by('-is_pinned', 'due_date', '-created_at')
        )
        data = TaskListSerializer(qs, many=True, context=_ctx(request)).data
        return Response({'tasks': data})


class MyRecentClosedTasksView(APIView):
    """GET tasks/recent-closed/ — tasks I completed/closed recently."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (
            Task.objects
            .filter(assigned_to=request.user, status__in=CLOSED_STATUSES)
            .select_related('assigned_by', 'category')
            .order_by('-completed_at', '-updated_at')[:50]
        )
        data = TaskListSerializer(qs, many=True, context=_ctx(request)).data
        return Response({'tasks': data})


class TaskDetailView(APIView):
    """GET tasks/<id>/ — full detail. Only the assignee may view via mobile."""
    permission_classes = [IsAuthenticated]

    def get(self, request, task_id):
        task = get_object_or_404(Task, id=task_id)
        if task.assigned_to_id != request.user.id:
            return Response({'error': 'Not your task.'}, status=403)
        data = TaskDetailSerializer(task, context=_ctx(request)).data
        return Response(data)


class TaskOnSiteView(APIView):
    """POST tasks/<id>/on-site/ — mark arrival. Body: gps_latitude, gps_longitude, note."""
    permission_classes = [IsAuthenticated]

    def post(self, request, task_id):
        task = get_object_or_404(Task, id=task_id)
        if task.assigned_to_id != request.user.id:
            return Response({'error': 'Not your task.'}, status=403)

        moved = task.mark_on_site(by_user=request.user)

        # Best-effort OnSiteEvent + fan-out, mirroring the web view.
        try:
            from apps.tasks.views_on_site import (
                _gps_from_post, _contract_for_task, _related_assignment,
                _related_visit, _fan_out_on_site,
            )
            lat, lng = _gps_from_post(request.data)
            note = (request.data.get('note') or '').strip()[:1000]
            contract = _contract_for_task(task)
            assignment = _related_assignment(task)
            visit = _related_visit(task)

            from apps.customer_portal.models import OnSiteEvent
            event = OnSiteEvent.objects.create(
                task=task, assignment=assignment, contract=contract,
                event_type='arrive', actor_user=request.user,
                gps_latitude=lat, gps_longitude=lng, note=note,
            )
            if visit:
                visit.status = 'on_site'
                if not visit.dispatched_at:
                    visit.dispatched_at = timezone.now()
                visit.technician = visit.technician or request.user
                visit.save(update_fields=['status', 'dispatched_at', 'technician', 'updated_at'])
            if event is not None:
                transaction.on_commit(lambda: _fan_out_on_site(event, contract))
        except Exception:
            logger.exception('mobile on_site: side-effects failed (arrival still recorded)')

        return Response({
            'status': 'ok',
            'on_site_at': task.on_site_at.isoformat() if task.on_site_at else None,
            'moved': moved,
        })


class TaskClearOnSiteView(APIView):
    """POST tasks/<id>/clear-on-site/ — log a depart event."""
    permission_classes = [IsAuthenticated]

    def post(self, request, task_id):
        task = get_object_or_404(Task, id=task_id)
        if task.assigned_to_id != request.user.id:
            return Response({'error': 'Not your task.'}, status=403)

        task.clear_on_site()
        try:
            from apps.tasks.views_on_site import _contract_for_task, _related_assignment
            from apps.customer_portal.models import OnSiteEvent
            OnSiteEvent.objects.create(
                task=task,
                assignment=_related_assignment(task),
                contract=_contract_for_task(task),
                event_type='depart',
                actor_user=request.user,
            )
        except Exception:
            logger.exception('mobile clear_on_site: event write failed')

        return Response({'status': 'ok'})


class TaskCompleteView(APIView):
    """
    POST tasks/<id>/complete/ — fulfill & close.

    Body: completion_comment (required).

    Customer-aware: customer-facing tasks go to CS verification
    (mark_done_pending_verify) exactly like the web flow; plain tasks
    are set done.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, task_id):
        task = get_object_or_404(Task, id=task_id)
        if task.assigned_to_id != request.user.id:
            return Response({'error': 'Not your task.'}, status=403)

        completion_comment = (request.data.get('completion_comment') or '').strip()
        if not completion_comment:
            return Response(
                {'error': 'A completion comment is required.'}, status=400,
            )

        try:
            from apps.tasks.views_on_site import (
                _related_assignment, _related_visit, _contract_for_task,
                _contact_emails_for_contract, _resolve_cs_owner,
                _notify_pending_verify,
            )
            assignment = _related_assignment(task)
            visit = _related_visit(task)
        except Exception:
            assignment = visit = None

        is_customer_facing = bool(task.customer_visible or assignment or visit)

        if is_customer_facing:
            task.mark_done_pending_verify()
        else:
            task.status = task.Status.DONE
            task.completed_at = timezone.now()
            task.progress_pct = 100
            task.save(update_fields=['status', 'completed_at', 'progress_pct', 'updated_at'])

        # Completion comment for the audit trail.
        try:
            from apps.tasks.models import TaskComment
            TaskComment.objects.create(
                task=task, author=request.user,
                content=f'✅ Completion note:\n{completion_comment}',
            )
        except Exception:
            logger.exception('mobile complete: comment write failed')

        if visit:
            try:
                visit.status = 'completed'
                visit.completed_at = timezone.now()
                visit.save(update_fields=['status', 'completed_at', 'updated_at'])
            except Exception:
                logger.exception('mobile complete: visit mirror failed')

        if is_customer_facing:
            try:
                contract = _contract_for_task(task)
                cs_owner = _resolve_cs_owner(task, assignment)
                emails = _contact_emails_for_contract(contract)
                transaction.on_commit(lambda: _notify_pending_verify(task, cs_owner, emails))
            except Exception:
                logger.exception('mobile complete: pending-verify notify failed')

        data = TaskDetailSerializer(task, context=_ctx(request)).data
        return Response({
            'status': 'ok',
            'customer_facing': is_customer_facing,
            'task': data,
        })