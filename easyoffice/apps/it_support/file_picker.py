"""
apps/it_support/file_picker.py
===============================

Bridge from maintenance evidence to the files app (apps.files.SharedFile).

Lets a technician attach an intake photo or document that ALREADY lives in
the files app — a scanned handover form, a warranty PDF, a photo the front
desk uploaded — instead of downloading it and re-uploading a duplicate.

Access control
──────────────
This is the sharp edge. A picker that lists files the user can't otherwise
open is a data leak, so this module NEVER builds its own visibility rules:
it delegates to apps.files.views._visible_files_qs, the same helper the file
manager itself uses. If the rules change there, they change here.

Note _visible_files_qs does a Python-level pass over candidate files to
resolve folder-inherited sharing, which is fine for the file manager but
would be wasteful per-keystroke. We always search a narrowed queryset and
cap results — see search_files.

Fail soft: if apps.files is unavailable the picker reports itself
unavailable and intake falls back to plain uploads.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SEARCH_LIMIT = 20

# Evidence is photos and documents. Excluding archives/executables keeps the
# picker useful — a technician attaching a .zip as "intake condition photo"
# is almost always a mistake, and they can still upload one if they mean it.
ALLOWED_EXTENSIONS = {
    'pdf',
    'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'heic', 'heif', 'tiff', 'tif',
    'doc', 'docx', 'txt', 'rtf', 'odt',
    'xls', 'xlsx', 'csv',
}


def _files_app():
    """Return (SharedFile, visible_files_qs) or (None, None)."""
    try:
        from apps.files.models import SharedFile
        from apps.files.views import _visible_files_qs
        return SharedFile, _visible_files_qs
    except Exception:
        return None, None


def files_app_available() -> bool:
    return _files_app()[0] is not None


def visible_files_for(user):
    """
    Every file `user` may see, per the files app's own rules.

    Superusers bypass the per-file walk — _visible_files_qs would otherwise
    loop the whole table in Python to resolve folder inheritance.
    """
    SharedFile, visible_files_qs = _files_app()
    if SharedFile is None:
        return None

    if getattr(user, 'is_superuser', False):
        return SharedFile.objects.filter(is_latest=True)

    return visible_files_qs(user).filter(is_latest=True)


def search_files(user, query='', *, limit=SEARCH_LIMIT, extensions=None):
    """
    Search the user's visible files for the attachment picker.

    Matches name, description and tags. Returns newest-first when the query
    is empty, so the picker opens on recent files — usually what someone
    wants when they uploaded the scan two minutes ago.
    """
    SharedFile, _ = _files_app()
    if SharedFile is None:
        return []

    try:
        qs = visible_files_for(user)
        if qs is None:
            return []

        from django.db.models import Q

        query = (query or '').strip()
        if query:
            qs = qs.filter(
                Q(name__icontains=query) |
                Q(description__icontains=query) |
                Q(tags__icontains=query)
            )

        allowed = extensions if extensions is not None else ALLOWED_EXTENSIONS
        if allowed:
            ext_q = Q()
            for ext in allowed:
                ext_q |= Q(name__iendswith=f'.{ext}')
            qs = qs.filter(ext_q)

        return list(
            qs.select_related('folder', 'uploaded_by')
              .order_by('-created_at')[:limit]
        )
    except Exception:
        logger.exception('File picker search failed')
        return []


def get_file_for_user(user, file_id):
    """
    Fetch one file IF the user may see it, else None.

    Never trust a file_id posted from the browser: re-check visibility
    server-side. Without this, anyone could attach any file in the system by
    guessing a UUID.
    """
    SharedFile, _ = _files_app()
    if SharedFile is None or not file_id:
        return None
    try:
        qs = visible_files_for(user)
        if qs is None:
            return None
        return qs.filter(pk=file_id).first()
    except Exception:
        logger.exception('File picker lookup failed for %s', file_id)
        return None


def _human_size(num_bytes):
    size = float(num_bytes or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} GB'


def serialize_file(shared_file) -> dict:
    """Compact JSON payload for the picker UI."""
    name = shared_file.name or ''
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    return {
        'id':          str(shared_file.pk),
        'name':        name,
        'extension':   ext,
        'is_image':    ext in {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'heic', 'heif'},
        'is_pdf':      ext == 'pdf',
        'size':        _human_size(shared_file.file_size),
        'folder':      shared_file.folder.name if shared_file.folder_id else 'Root',
        'uploaded_by': getattr(shared_file.uploaded_by, 'full_name', '') or
                       getattr(shared_file.uploaded_by, 'username', ''),
        'created_at':  shared_file.created_at.strftime('%d %b %Y'),
    }
