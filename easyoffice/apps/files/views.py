from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView, View
from django.shortcuts import render, redirect, get_object_or_404
from django.http import FileResponse, Http404
from django.contrib import messages
from django.db.models import Q, Sum
from apps.files.models import SharedFile, FileFolder


def _visible_files_qs(user):
    """Base queryset of files this user can see."""
    profile = getattr(user, 'staffprofile', None)
    unit = profile.unit if profile else None
    dept = profile.department if profile else None
    return SharedFile.objects.filter(
        Q(uploaded_by=user) |
        Q(visibility='office') |
        Q(visibility='unit', unit=unit) |
        Q(visibility='department', department=dept) |
        Q(shared_with=user)
    ).distinct()


def _visible_folders_qs(user):
    profile = getattr(user, 'staffprofile', None)
    unit = profile.unit if profile else None
    return FileFolder.objects.filter(
        Q(owner=user) | Q(visibility='office') |
        Q(visibility='unit', unit=unit)
    ).distinct()


# ---------------------------------------------------------------------------
# File Manager — main view
# ---------------------------------------------------------------------------

class FileManagerView(LoginRequiredMixin, TemplateView):
    template_name = 'files/file_manager.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        request = self.request

        files = _visible_files_qs(user).select_related('uploaded_by', 'folder')
        folders = _visible_folders_qs(user).select_related('owner', 'parent')

        # ── Filters ──
        q = request.GET.get('q', '').strip()
        filter_mode = request.GET.get('filter', '')   # mine | shared | recent | office
        type_cat = request.GET.get('type', '')         # document | image | …
        folder_id = request.GET.get('folder', '')
        sort = request.GET.get('sort', '-created_at')

        current_folder = None
        if folder_id:
            try:
                current_folder = folders.get(id=folder_id)
                files = files.filter(folder=current_folder)
                folders = folders.filter(parent=current_folder)
            except FileFolder.DoesNotExist:
                current_folder = None
        else:
            # Root: only show files with no folder
            if not q and not filter_mode and not type_cat:
                # Show all files but filter folders to root level
                folders = folders.filter(parent__isnull=True)

        if q:
            files = files.filter(
                Q(name__icontains=q) | Q(description__icontains=q) | Q(tags__icontains=q)
            )
        if filter_mode == 'mine':
            files = files.filter(uploaded_by=user)
            folders = folders.filter(owner=user)
        elif filter_mode == 'shared':
            files = files.filter(shared_with=user)
        elif filter_mode == 'office':
            files = files.filter(visibility='office')
        elif filter_mode == 'recent':
            from django.utils import timezone
            from datetime import timedelta
            files = files.filter(created_at__gte=timezone.now() - timedelta(days=30))
        if type_cat:
            from apps.files.models import _TYPE_CATEGORY
            exts = _TYPE_CATEGORY.get(type_cat, set())
            if exts:
                ext_q = Q()
                for ext in exts:
                    ext_q |= Q(name__iendswith=f'.{ext}')
                files = files.filter(ext_q)

        # Sort
        allowed_sorts = {
            'name': 'name', '-name': '-name',
            'created_at': 'created_at', '-created_at': '-created_at',
            'file_size': 'file_size', '-file_size': '-file_size',
            'download_count': 'download_count', '-download_count': '-download_count',
        }
        sort = allowed_sorts.get(sort, '-created_at')
        files = files.order_by(sort)

        # Stats
        my_files_qs = _visible_files_qs(user).filter(uploaded_by=user)
        my_total_size = my_files_qs.aggregate(s=Sum('file_size'))['s'] or 0
        my_file_count = my_files_qs.count()
        office_count = _visible_files_qs(user).filter(visibility='office').count()
        shared_count = _visible_files_qs(user).filter(shared_with=user).count()

        from apps.core.models import User as CoreUser
        from apps.organization.models import Unit, Department
        ctx.update({
            'files': files,
            'folders': folders,
            'current_folder': current_folder,
            'folder_ancestors': current_folder.ancestors() if current_folder else [],
            'all_folders': _visible_folders_qs(user).order_by('name'),
            'all_staff': CoreUser.objects.filter(
                is_active=True, status='active'
            ).exclude(id=user.id).select_related('staffprofile', 'staffprofile__position').order_by('first_name'),
            'all_units': Unit.objects.filter(is_active=True).order_by('name'),
            'all_departments': Department.objects.filter(is_active=True).order_by('name'),
            'visibility_choices': SharedFile.Visibility.choices,
            'type_categories': [
                ('document', 'Documents', 'bi-file-earmark-text'),
                ('spreadsheet', 'Spreadsheets', 'bi-file-earmark-excel'),
                ('presentation', 'Presentations', 'bi-file-earmark-slides'),
                ('image', 'Images', 'bi-file-earmark-image'),
                ('video', 'Videos', 'bi-file-earmark-play'),
                ('audio', 'Audio', 'bi-file-earmark-music'),
                ('archive', 'Archives', 'bi-file-earmark-zip'),
                ('code', 'Code', 'bi-file-earmark-code'),
            ],
            # Active filters
            'q': q,
            'filter_mode': filter_mode,
            'type_cat': type_cat,
            'folder_id': folder_id,
            'sort': sort,
            # Stats
            'my_file_count': my_file_count,
            'my_total_size': my_total_size,
            'office_count': office_count,
            'shared_count': shared_count,
            'total_file_count': _visible_files_qs(user).count(),
        })
        return ctx


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

class FileUploadView(LoginRequiredMixin, View):
    def post(self, request):
        f = request.FILES.get('file')
        if not f:
            messages.error(request, 'No file selected.')
            return redirect('file_manager')

        folder_id = request.POST.get('folder_id')
        folder = None
        if folder_id:
            try:
                folder = FileFolder.objects.get(id=folder_id)
            except FileFolder.DoesNotExist:
                pass

        SharedFile.objects.create(
            name=request.POST.get('name', f.name).strip() or f.name,
            file=f,
            folder=folder,
            uploaded_by=request.user,
            visibility=request.POST.get('visibility', 'private'),
            description=request.POST.get('description', ''),
            tags=request.POST.get('tags', ''),
            file_size=f.size,
            file_type=f.content_type,
        )
        messages.success(request, f'"{f.name}" uploaded successfully.')

        redirect_url = request.POST.get('next', '')
        if redirect_url and redirect_url.startswith('/'):
            return redirect(redirect_url)
        if folder_id:
            return redirect(f'{request.build_absolute_uri("/files/")}?folder={folder_id}')
        return redirect('file_manager')


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

class FileDownloadView(LoginRequiredMixin, View):
    def get(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk)
        # Access check
        user = request.user
        profile = getattr(user, 'staffprofile', None)
        unit = profile.unit if profile else None
        dept = profile.department if profile else None
        has_access = (
            f.uploaded_by == user or
            f.visibility == 'office' or
            (f.visibility == 'unit' and f.unit == unit) or
            (f.visibility == 'department' and f.department == dept) or
            f.shared_with.filter(id=user.id).exists()
        )
        if not has_access:
            raise Http404
        f.download_count += 1
        f.save(update_fields=['download_count'])
        return FileResponse(f.file.open('rb'), as_attachment=True, filename=f.name)


# ---------------------------------------------------------------------------
# Share / Update visibility of an existing file
# ---------------------------------------------------------------------------

class FileShareView(LoginRequiredMixin, View):
    def get(self, request, pk):
        """Return share context — used if you ever want a standalone share page."""
        return redirect('file_manager')

    def post(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk, uploaded_by=request.user)

        visibility = request.POST.get('visibility', f.visibility)
        f.visibility = visibility
        f.save(update_fields=['visibility'])

        # Rebuild shared_with list
        f.shared_with.clear()
        for uid in request.POST.getlist('shared_with'):
            try:
                from apps.core.models import User
                u = User.objects.get(id=uid)
                f.shared_with.add(u)
            except Exception:
                pass

        # Assign unit / department if relevant
        if visibility == 'unit':
            unit_id = request.POST.get('unit_id')
            if unit_id:
                try:
                    from apps.organization.models import Unit
                    f.unit = Unit.objects.get(id=unit_id)
                    f.save(update_fields=['unit'])
                except Exception:
                    pass
        elif visibility == 'department':
            dept_id = request.POST.get('dept_id')
            if dept_id:
                try:
                    from apps.organization.models import Department
                    f.department = Department.objects.get(id=dept_id)
                    f.save(update_fields=['department'])
                except Exception:
                    pass

        messages.success(request, f'Sharing settings updated for "{f.name}".')
        next_url = request.POST.get('next', '')
        if next_url and next_url.startswith('/'):
            return redirect(next_url)
        return redirect('file_manager')




class FileDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        f = get_object_or_404(SharedFile, pk=pk, uploaded_by=request.user)
        name = f.name
        folder_id = f.folder_id
        f.file.delete(save=False)
        f.delete()
        messages.success(request, f'"{name}" deleted.')
        if folder_id:
            return redirect(f'/files/?folder={folder_id}')
        return redirect('file_manager')


# ---------------------------------------------------------------------------
# Create Folder
# ---------------------------------------------------------------------------

class FolderCreateView(LoginRequiredMixin, View):
    def post(self, request):
        name = request.POST.get('name', '').strip()
        if not name:
            messages.error(request, 'Folder name is required.')
            return redirect('file_manager')

        parent_id = request.POST.get('parent_id')
        parent = None
        if parent_id:
            try:
                parent = FileFolder.objects.get(id=parent_id, owner=request.user)
            except FileFolder.DoesNotExist:
                pass

        FileFolder.objects.create(
            name=name,
            owner=request.user,
            parent=parent,
            visibility=request.POST.get('visibility', 'private'),
            color=request.POST.get('color', '#f59e0b'),
        )
        messages.success(request, f'Folder "{name}" created.')
        redirect_url = request.POST.get('next', 'file_manager')
        return redirect(redirect_url if redirect_url.startswith('/') else 'file_manager')


# ---------------------------------------------------------------------------
# Delete Folder
# ---------------------------------------------------------------------------

class FolderDeleteView(LoginRequiredMixin, View):
    def post(self, request, pk):
        folder = get_object_or_404(FileFolder, pk=pk, owner=request.user)
        name = folder.name
        parent_id = folder.parent_id
        folder.delete()
        messages.success(request, f'Folder "{name}" deleted.')
        if parent_id:
            return redirect(f'/files/?folder={parent_id}')
        return redirect('file_manager')


# ---------------------------------------------------------------------------
# Share / Update visibility of a folder
# ---------------------------------------------------------------------------

class FolderShareView(LoginRequiredMixin, View):
    def post(self, request, pk):
        folder = get_object_or_404(FileFolder, pk=pk, owner=request.user)

        visibility = request.POST.get('visibility', folder.visibility)
        allowed = {'private', 'unit', 'department', 'office'}
        folder.visibility = visibility if visibility in allowed else 'private'

        if folder.visibility == 'unit':
            unit_id = request.POST.get('unit_id')
            if unit_id:
                try:
                    from apps.organization.models import Unit
                    folder.unit = Unit.objects.get(id=unit_id)
                except Exception:
                    pass
        elif folder.visibility == 'department':
            dept_id = request.POST.get('dept_id')
            if dept_id:
                try:
                    from apps.organization.models import Department
                    folder.department = Department.objects.get(id=dept_id)
                except Exception:
                    pass

        folder.save()
        messages.success(request, f'Sharing updated for folder "{folder.name}".')
        next_url = request.POST.get('next', '')
        if next_url and next_url.startswith('/'):
            return redirect(next_url)
        return redirect('file_manager')