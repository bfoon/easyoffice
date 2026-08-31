from django.urls import path
from apps.dashboard import views
from apps.dashboard import views_favourites as fav

urlpatterns = [
    path('', views.DashboardView.as_view(), name='dashboard'),
    path('office-admin/', views.AdminDashboardView.as_view(), name='admin_dashboard'),
    path('task-followup/', views.TaskFollowUpView.as_view(), name='task_followup'),

    # ── Favourites (pinned links on the dashboard) ───────────────────────
    path('favourites/',         fav.FavouriteListView.as_view(),    name='favourites'),
    path('favourites/add/',     fav.FavouriteAddView.as_view(),     name='favourite_add'),
    path('favourites/remove/',  fav.FavouriteRemoveView.as_view(),  name='favourite_remove'),
    path('favourites/reorder/', fav.FavouriteReorderView.as_view(), name='favourite_reorder'),
]