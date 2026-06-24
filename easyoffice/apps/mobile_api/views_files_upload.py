"""
apps/mobile_api/views_files_upload.py
─────────────────────────────────────
Upload a file from the phone's filesystem straight into the Files app
(apps.files.SharedFile). Mirrors the web SaveChatMessageToFilesView field-for-
field, so a phone-uploaded file is indistinguishable from one saved on the web:
private visibility, owned by the uploader, optional target folder, hash computed.

Route (add to apps/mobile_api/urls.py):
    path('files/upload/', views_files_upload.MobileFileUploadView.as_view(),
         name='mobile_file_upload'),
"""

import logging
import os

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser

log = logging.getLogger(__name__)


class MobileFileUploadView(APIView):
    """
    POST /api/mobile/v1/files/upload/
    multipart body:
        file    (required)  the file bytes
        folder  (optional)  target FileFolder id (must be owned by the user)

    Creates a private SharedFile owned by request.user, the same way the web
    "save to Files" flow does.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        from apps.files.models import SharedFile, FileFolder

        f = request.FILES.get('file')
        if not f:
            return Response({'error': 'No file provided.'}, status=400)

        # Optional folder, must belong to this user.
        folder = None
        folder_id = (request.data.get('folder') or '').strip()
        if folder_id:
            folder = FileFolder.objects.filter(id=folder_id, owner=request.user).first()
            if folder is None:
                return Response({'error': 'Folder not found.'}, status=404)

        name = f.name or 'upload'
        mime_type = getattr(f, 'content_type', None) or 'application/octet-stream'

        try:
            saved = SharedFile.objects.create(
                name=name,
                uploaded_by=request.user,
                folder=folder,
                visibility='private',
                description='Uploaded from mobile',
                file_size=f.size,
                file_type=mime_type,
            )
            saved.file.save(os.path.basename(name), f, save=True)
        except Exception as e:
            log.exception('mobile file upload failed')
            return Response({'error': 'Upload failed.'}, status=500)

        # Best-effort hash, mirroring the web flow.
        try:
            saved.file_hash = saved.compute_hash()
            saved.save(update_fields=['file_hash'])
        except Exception:
            pass

        try:
            url = request.build_absolute_uri(saved.file.url)
        except Exception:
            url = ''

        return Response({
            'ok': True,
            'file': {
                'id': str(saved.id),
                'name': saved.name,
                'url': url,
                'size': saved.file_size,
                'folder_id': str(saved.folder_id) if saved.folder_id else None,
            },
        }, status=201)
