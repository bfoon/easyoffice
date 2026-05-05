from django.urls import path
from apps.tasks import views
from apps.tasks import views_on_site

urlpatterns = [
    path('', views.TaskListView.as_view(), name='task_list'),
    path('create/', views.TaskCreateView.as_view(), name='task_create'),
    path('<uuid:pk>/', views.TaskDetailView.as_view(), name='task_detail'),
    path('<uuid:pk>/edit/', views.TaskUpdateView.as_view(), name='task_edit'),
    path('<uuid:pk>/quick-update/', views.TaskQuickUpdateView.as_view(), name='task_quick_update'),
    path('<uuid:pk>/comment/', views.TaskCommentView.as_view(), name='task_comment'),
    path('<uuid:pk>/reassign/', views.TaskReassignView.as_view(), name='task_reassign'),
    path('<uuid:pk>/timelog/', views.TaskTimeLogView.as_view(), name='task_timelog'),
    path(
            '<uuid:pk>/on-site/',
            views_on_site.TaskOnSiteView.as_view(),
            name='task_on_site',
        ),
    path(
        '<uuid:pk>/clear-on-site/',
        views_on_site.TaskClearOnSiteView.as_view(),
        name='task_clear_on_site',
    ),
    path(
        '<uuid:pk>/mark-done/',
        views_on_site.TaskMarkDoneView.as_view(),
        name='task_mark_done',
    ),

]