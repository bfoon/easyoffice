"""
apps/files/folder_upload.py
───────────────────────────
Uploading a whole folder, with its subfolders, in one go.

How it works
────────────
Browsers cannot post a directory. What they give you is a flat list of files,
each carrying the path it had on disk in ``File.webkitRelativePath``:

    Reports/2026/Q1/summary.pdf
    Reports/2026/Q1/annex-a.xlsx
    Reports/2026/Q2/summary.pdf

So the tree is rebuilt here: split each path, create or reuse a FileFolder per
segment, and drop the file in the leaf. The client posts files in batches with
a parallel ``paths`` list; every batch reuses folders created by earlier ones,
because folder lookup is keyed on (name, parent, owner).

Why a separate endpoint rather than looping FileUploadView
──────────────────────────────────────────────────────────
FileUploadView takes one file and one already-existing folder id. A 300-file
folder would mean 300 round trips, 300 messages framework calls, and no way to
create the intermediate directories. It also has no notion of a path, so
everything would land flat in one folder.

Safety
──────
Paths come from the client, so they are treated as hostile:

  • ``..`` and absolute paths are rejected outright — a traversal here would
    let someone graft folders onto another user's tree;
  • every segment goes through ``clean_display_name`` (strips control
    characters, path separators and Windows-hostile punctuation);
  • depth, file count and total size are capped;
  • the destination folder is permission-checked once, up front. The old
    FileUploadView did ``FileFolder.objects.get(id=folder_id)`` with no check
    at all, which let any signed-in user upload into anyone's folder.

Settings (all optional)
───────────────────────
    FILES_FOLDER_UPLOAD_MAX_DEPTH  = 12
    FILES_FOLDER_UPLOAD_MAX_FILES  = 500        # per batch
    FILES_FOLDER_UPLOAD_MAX_BYTES  = 500 * 1024 * 1024
"""
from __future__ import annotations

import logging
import os

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.views.generic import View

from apps.files.models import FileFolder, SharedFile
from apps.files.security_utils import clean_display_name

logger = logging.getLogger(__name__)

MAX_DEPTH = getattr(settings, 'FILES_FOLDER_UPLOAD_MAX_DEPTH', 12)
MAX_FILES = getattr(settings, 'FILES_FOLDER_UPLOAD_MAX_FILES', 500)
MAX_BYTES = getattr(settings, 'FILES_FOLDER_UPLOAD_MAX_BYTES', 500 * 1024 * 1024)

# Junk the OS scatters through directories. Uploading a folder should not
# recreate the file manager's own droppings on the server.
IGNORED_NAMES = {
    '.ds_store', 'thumbs.db', 'desktop.ini', '.localized', 'icon\r',
}
IGNORED_DIRS = {'__macosx', '.git', 'node_modules', '.svn', '.idea'}


# ── Path handling ───────────────────────────────────────────────────────────

def split_relative_path(raw_path: str):
    """
    Turn a browser-supplied relative path into a safe list of segments.

    Returns ``(folder_segments, filename)``, or ``(None, None)`` when the path
    should be rejected or skipped.
    """
    path = (raw_path or '').replace('\\', '/').strip()
    if not path:
        return None, None

    # Absolute paths and drive letters have no business here.
    if path.startswith('/') or (len(path) > 1 and path[1] == ':'):
        return None, None

    raw_segments = [seg for seg in path.split('/') if seg not in ('', '.')]
    if not raw_segments:
        return None, None

    if any(seg == '..' for seg in raw_segments):
        return None, None

    *dir_parts, file_part = raw_segments

    if file_part.lower() in IGNORED_NAMES:
        return None, None
    if any(seg.lower() in IGNORED_DIRS for seg in dir_parts):
        return None, None

    cleaned_dirs = []
    for seg in dir_parts[:MAX_DEPTH]:
        cleaned = clean_display_name(seg, max_length=120)
        # clean_display_name returns 'Untitled' for a segment that was made
        # entirely of stripped characters — keep it rather than silently
        # flattening the tree.
        cleaned_dirs.append(cleaned)

    filename = clean_display_name(file_part, max_length=255)
    if not filename:
        return None, None

    return cleaned_dirs, filename


# ── Folder tree ─────────────────────────────────────────────────────────────

class FolderTreeBuilder:
    """
    Creates folders on demand and remembers them, so a 400-file upload does
    one query per distinct directory rather than one per file.
    """

    def __init__(self, user, root=None, visibility='private', color='#f59e0b'):
        self.user = user
        self.root = root
        self.visibility = visibility
        self.color = color
        self._cache = {(): root}
        self.created = []

    def ensure(self, segments):
        key = tuple(segments)
        if key in self._cache:
            return self._cache[key]

        parent = self.ensure(segments[:-1])
        name = segments[-1]

        folder = FileFolder.objects.filter(
            name=name, parent=parent, owner=self.user,
        ).first()

        if folder is None:
            folder = FileFolder.objects.create(
                name=name,
                parent=parent,
                owner=self.user,
                visibility=self.visibility,
                color=self.color,
            )
            self.created.append(folder)

        self._cache[key] = folder
        return folder


# ── The view ────────────────────────────────────────────────────────────────

class FolderUploadView(LoginRequiredMixin, View):
    """
    POST /files/upload-folder/

    Form fields
    ───────────
      files[]      one or more files
      paths[]      the relative path of each file, same order and length
      folder_id    optional destination folder (must be writable)
      project_id   optional project to tag every file with
      visibility   applied to created folders and files (default 'private')

    Responds with a per-batch summary so the client can show progress and
    report partial failures without abandoning the whole upload.
    """

    def post(self, request):
        files = request.FILES.getlist('files[]') or request.FILES.getlist('files')
        paths = request.POST.getlist('paths[]') or request.POST.getlist('paths')

        if not files:
            return JsonResponse({'ok': False, 'error': 'No files received.'}, status=400)

        if len(paths) != len(files):
            return JsonResponse({
                'ok': False,
                'error': 'Each file must be sent with its relative path.',
            }, status=400)

        if len(files) > MAX_FILES:
            return JsonResponse({
                'ok': False,
                'error': f'Too many files in one batch (limit {MAX_FILES}).',
            }, status=400)

        total_bytes = sum(getattr(f, 'size', 0) or 0 for f in files)
        if total_bytes > MAX_BYTES:
            return JsonResponse({
                'ok': False,
                'error': (
                    f'Batch is too large ({total_bytes // (1024 * 1024)} MB); '
                    f'limit is {MAX_BYTES // (1024 * 1024)} MB.'
                ),
            }, status=400)

        # ── Destination ─────────────────────────────────────────────────────
        root = None
        folder_id = (request.POST.get('folder_id') or '').strip()
        if folder_id:
            from apps.files.views import _can_edit_folder
            root = FileFolder.objects.filter(id=folder_id).first()
            if root is None:
                return JsonResponse(
                    {'ok': False, 'error': 'Destination folder not found.'}, status=404
                )
            if not _can_edit_folder(request.user, root):
                return JsonResponse(
                    {'ok': False, 'error': 'You cannot upload into that folder.'},
                    status=403,
                )

        project = None
        project_id = (request.POST.get('project_id') or '').strip()
        if project_id:
            try:
                from apps.projects.models import Project
                project = Project.objects.filter(id=project_id).first()
            except Exception:
                project = None

        visibility = request.POST.get('visibility', 'private')

        builder = FolderTreeBuilder(
            request.user, root=root, visibility=visibility,
            color=request.POST.get('color', '#f59e0b'),
        )

        stored = 0
        skipped = []
        failed = []

        for upload, raw_path in zip(files, paths):
            # A file dragged in without a path is still worth keeping — put it
            # at the root rather than dropping it.
            candidate = raw_path or getattr(upload, 'name', '')
            segments, filename = split_relative_path(candidate)

            if filename is None:
                skipped.append(raw_path or getattr(upload, 'name', '?'))
                continue

            try:
                folder = builder.ensure(segments) if segments else root
            except Exception:
                logger.exception('Could not create folder path for %s', raw_path)
                failed.append(raw_path)
                continue

            try:
                sf = SharedFile.objects.create(
                    name=filename,
                    file=upload,
                    folder=folder,
                    project=project,
                    uploaded_by=request.user,
                    visibility=visibility,
                    file_size=getattr(upload, 'size', 0) or 0,
                    file_type=getattr(upload, 'content_type', '') or '',
                )
            except Exception:
                logger.exception('Could not store %s', raw_path)
                failed.append(raw_path)
                continue

            try:
                sf.file_hash = sf.compute_hash()
                sf.save(update_fields=['file_hash'])
            except Exception:
                pass

            stored += 1

        top_level = ''
        if builder.created:
            top_level = builder.created[0].name
        elif paths:
            first_segments, _ = split_relative_path(paths[0])
            if first_segments:
                top_level = first_segments[0]

        return JsonResponse({
            'ok': True,
            'stored': stored,
            'folders_created': len(builder.created),
            'skipped': skipped[:20],
            'skipped_count': len(skipped),
            'failed': failed[:20],
            'failed_count': len(failed),
            'top_level': top_level,
            'destination_id': str(root.id) if root else '',
            'message': _summary(stored, len(builder.created), len(skipped), len(failed)),
        })


def _summary(stored, folders, skipped, failed):
    bits = [f'{stored} file{"" if stored == 1 else "s"} uploaded']
    if folders:
        bits.append(f'{folders} folder{"" if folders == 1 else "s"} created')
    if skipped:
        bits.append(f'{skipped} skipped')
    if failed:
        bits.append(f'{failed} failed')
    return ', '.join(bits) + '.'
