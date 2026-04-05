from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm
from django.views.generic import View, TemplateView, UpdateView
from django.shortcuts import render, redirect
from django.contrib import messages
from django.http import JsonResponse
from django.urls import reverse_lazy
from django.utils import timezone
from apps.core.models import User, CoreNotification


class LoginView(View):
    template_name = 'auth/login.html'

    def get(self, request):
        if request.user.is_authenticated:
            return redirect('dashboard')
        return render(request, self.template_name, {'form': AuthenticationForm()})

    def post(self, request):
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            user.update_last_seen()
            next_url = request.GET.get('next', '/dashboard/')
            return redirect(next_url)
        return render(request, self.template_name, {'form': form})


class LogoutView(LoginRequiredMixin, View):
    def post(self, request):
        request.user.is_online = False
        request.user.save(update_fields=['is_online'])
        logout(request)
        return redirect('login')

    def get(self, request):
        return self.post(request)


class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = 'auth/profile.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx['recent_tasks'] = user.assigned_tasks.filter(
            status__in=['todo', 'in_progress']
        ).order_by('-created_at')[:5]
        ctx['recent_projects'] = user.projects.filter(
            status='active'
        ).order_by('-created_at')[:5]
        # Build skills list so template doesn't need .split ','
        raw = getattr(user, 'skills', '') or ''
        ctx['staff_skills'] = [s.strip() for s in raw.split(',') if s.strip()]
        return ctx


class ProfileEditView(LoginRequiredMixin, View):
    template_name = 'auth/profile_edit.html'

    def get(self, request):
        return render(request, self.template_name, {'user': request.user})

    def post(self, request):
        user = request.user
        user.first_name = request.POST.get('first_name', user.first_name)
        user.last_name = request.POST.get('last_name', user.last_name)
        user.phone = request.POST.get('phone', user.phone)
        user.bio = request.POST.get('bio', user.bio)
        user.linkedin_url = request.POST.get('linkedin_url', user.linkedin_url)
        user.skills = request.POST.get('skills', user.skills)
        user.timezone_pref = request.POST.get('timezone_pref', user.timezone_pref)
        user.theme = request.POST.get('theme', user.theme)
        user.notification_email = 'notification_email' in request.POST
        user.notification_push = 'notification_push' in request.POST
        if 'avatar' in request.FILES:
            user.avatar = request.FILES['avatar']
        user.save()
        messages.success(request, 'Profile updated successfully.')
        return redirect('profile')


class ChangePasswordView(LoginRequiredMixin, View):
    template_name = 'auth/change_password.html'

    def get(self, request):
        return render(request, self.template_name, {'form': PasswordChangeForm(request.user)})

    def post(self, request):
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            messages.success(request, 'Password changed successfully.')
            return redirect('profile')
        return render(request, self.template_name, {'form': form})


class UserSettingsView(LoginRequiredMixin, View):
    template_name = 'auth/settings.html'

    def get(self, request):
        return render(request, self.template_name)

    def post(self, request):
        user = request.user
        user.theme = request.POST.get('theme', user.theme)
        user.compact_view = 'compact_view' in request.POST
        user.notification_email = 'notification_email' in request.POST
        user.notification_push = 'notification_push' in request.POST
        user.language = request.POST.get('language', user.language)
        user.timezone_pref = request.POST.get('timezone_pref', user.timezone_pref)
        user.save()
        messages.success(request, 'Settings saved.')
        return redirect('user_settings')


class NotificationsView(LoginRequiredMixin, TemplateView):
    template_name = 'auth/notifications.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['notifications'] = self.request.user.core_notifications.all()[:50]
        return ctx


class MarkNotificationsReadView(LoginRequiredMixin, View):
    def post(self, request):
        request.user.core_notifications.filter(is_read=False).update(
            is_read=True, read_at=timezone.now()
        )
        return JsonResponse({'status': 'ok'})