"""
apps/tasks/views_attachments.py
================================

NEW MODULE — handles file/photo uploads on tasks, and sorted listing.

URL wiring (added to apps/tasks/urls.py):
    path('<uuid:pk>/attachments/upload/',
         views_attachments.TaskAttachmentUploadView.as_view(),
         name='task_attachment_upload'),
    path('<uuid:pk>/attachments/<uuid:att_pk>/delete/',
         views_attachments.TaskAttachmentDeleteView.as_view(),
         name='task_attachment_delete'),
"""
from __future__ import annotations

import logging
import mimetypes

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import View

from apps.tasks.models import Task, TaskAttachment

logger = logging.getLogger(__name__)


# Reuse the same permission helpers as views.py — but to keep this module
# self-contained, we import them lazily.
def _can_view_task(user, task):
    from apps.tasks.views import _can_view_task as _impl
    return _impl(user, task)


def _can_full_edit_task(user, task):
    from apps.tasks.views import _can_full_edit_task as _impl
    return _impl(user, task)


# Allow these MIME prefixes / extensions. Block executables / scripts.
ALLOWED_PREFIXES = (
    'image/', 'application/pdf', 'application/msword',
    'application/vnd.openxmlformats',  # docx, xlsx, pptx
    'application/vnd.ms-excel',
    'application/vnd.ms-powerpoint',
    'text/plain', 'text/csv',
    'application/zip', 'application/x-zip-compressed',
    'video/', 'audio/',
)
BLOCKED_EXTS = (
    '.exe', '.bat', '.cmd', '.sh', '.ps1', '.msi', '.scr',
    '.com', '.dll', '.so', '.app', '.apk', '.jar',
    '.js', '.vbs', '.wsf',
)
MAX_FILE_BYTES = 25 * 1024 * 1024  # 25 MB per file


def _is_allowed(uploaded_file) -> tuple[bool, str]:
    name = (uploaded_file.name or '').lower()
    for ext in BLOCKED_EXTS:
        if name.endswith(ext):
            return False, f'File type {ext} is not allowed for security reasons.'
    if uploaded_file.size and uploaded_file.size > MAX_FILE_BYTES:
        return False, f'File is too large (max {MAX_FILE_BYTES // (1024*1024)}MB per file).'

    ctype = uploaded_file.content_type or mimetypes.guess_type(name)[0] or ''
    if ctype and not any(ctype.startswith(p) for p in ALLOWED_PREFIXES):
        # Don't block — many legitimate files come through with weird MIME.
        # We only hard-block by extension above.
        pass
    return True, ''


# ─────────────────────────────────────────────────────────────────────────────
# Upload
# ─────────────────────────────────────────────────────────────────────────────

class TaskAttachmentUploadView(LoginRequiredMixin, View):
    """
    POST /tasks/<pk>/attachments/upload/

    Accepts multipart form with one or more `files` fields. Anyone who can
    view the task can attach files (assignee, collaborators, supervisor,
    creator, admins). This is intentionally permissive — uploading evidence
    of work is a normal flow.
    """
    http_method_names = ['post']

    def post(self, request, pk):
        task = get_object_or_404(Task, pk=pk)
        if not _can_view_task(request.user, task):
            return HttpResponseForbidden('You cannot attach files to this task.')

        files = request.FILES.getlist('files')
        if not files:
            messages.error(request, 'No files were selected.')
            return redirect('task_detail', pk=task.pk)

        saved = 0
        rejected = []
        for f in files:
            ok, reason = _is_allowed(f)
            if not ok:
                rejected.append(f'{f.name}: {reason}')
                continue
            try:
                TaskAttachment.objects.create(
                    task=task,
                    uploaded_by=request.user,
                    file=f,
                    file_name=f.name[:255],
                    file_size=f.size or 0,
                    file_type=f.content_type or '',
                )
                saved += 1
            except Exception:
                logger.exception('attachment upload failed for task %s', task.pk)
                rejected.append(f'{f.name}: server error')

        # Update denormalised count
        if saved:
            task.attachments_count = task.attachments.count()
            task.save(update_fields=['attachments_count', 'updated_at'])

            # Notify the watchers — uploading a file is meaningful activity
            try:
                from apps.tasks.views import _notify_task_watchers
                _notify_task_watchers(
                    task,
                    request.user,
                    'Files attached',
                    f'{request.user.full_name} attached {saved} file(s) to "{task.title}".',
                )
            except Exception:
                logger.exception('attachment upload: notify failed')

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'status': 'ok' if saved else 'error',
                'saved': saved,
                'rejected': rejected,
            })

        if saved:
            messages.success(request, f'Attached {saved} file(s).')
        if rejected:
            messages.warning(
                request,
                'Some files were not attached: ' + '; '.join(rejected),
            )
        return redirect('task_detail', pk=task.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Delete
# ─────────────────────────────────────────────────────────────────────────────

class TaskAttachmentDeleteView(LoginRequiredMixin, View):
    """
    POST /tasks/<pk>/attachments/<att_pk>/delete/

    The uploader can always delete their own attachment. Admins and the
    task creator can delete anyone's attachment.
    """
    http_method_names = ['post']

    def post(self, request, pk, att_pk):
        task = get_object_or_404(Task, pk=pk)
        attachment = get_object_or_404(TaskAttachment, pk=att_pk, task=task)

        is_uploader = (attachment.uploaded_by_id == request.user.id)
        if not (is_uploader or _can_full_edit_task(request.user, task)):
            return HttpResponseForbidden('You cannot delete this attachment.')

        name = attachment.file_name
        try:
            attachment.file.delete(save=False)
        except Exception:
            logger.exception('failed to delete attachment file from storage')
        attachment.delete()

        task.attachments_count = task.attachments.count()
        task.save(update_fields=['attachments_count', 'updated_at'])

        messages.success(request, f'Removed “{name}”.')
        return redirect('task_detail', pk=task.pk)


# ─────────────────────────────────────────────────────────────────────────────
# Sorting helper (used by TaskDetailView context)
# ─────────────────────────────────────────────────────────────────────────────

ATTACHMENT_SORT_CHOICES = {
    # key: (label, queryset .order_by args)
    'newest':    ('Newest first',    ('-uploaded_at',)),
    'oldest':    ('Oldest first',    ('uploaded_at',)),
    'name_az':   ('Name A→Z',        ('file_name',)),
    'name_za':   ('Name Z→A',        ('-file_name',)),
    'owner':     ('By owner',        ('uploaded_by__first_name', 'uploaded_by__last_name', '-uploaded_at')),
    'size_big':  ('Largest first',   ('-file_size',)),
    'size_small':('Smallest first',  ('file_size',)),
    'type':      ('By file type',    ('file_type', '-uploaded_at')),
}


def sorted_attachments(task, sort_key: str = 'newest'):
    """
    Return the task's attachments queryset ordered per `sort_key`. Falls
    back to 'newest' if the key is unknown. Use this in TaskDetailView:

        sort = self.request.GET.get('att_sort', 'newest')
        ctx['attachments'] = sorted_attachments(task, sort)
        ctx['attachment_sort'] = sort
        ctx['attachment_sort_choices'] = ATTACHMENT_SORT_CHOICES
    """
    order = ATTACHMENT_SORT_CHOICES.get(sort_key, ATTACHMENT_SORT_CHOICES['newest'])[1]
    return task.attachments.select_related('uploaded_by').order_by(*order)
