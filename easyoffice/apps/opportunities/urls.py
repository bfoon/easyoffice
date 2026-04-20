from django.urls import path
from . import views

urlpatterns = [
    path('', views.OpportunityDashboardView.as_view(), name='opportunity_list'),

    path('scan-all/', views.OpportunityScanAllView.as_view(), name='opportunity_scan_all'),

    path('sources/', views.OpportunitySourceListView.as_view(), name='opportunity_source_list'),
    path('sources/create/', views.OpportunitySourceCreateView.as_view(), name='opportunity_source_create'),
    path('sources/<uuid:pk>/edit/', views.OpportunitySourceUpdateView.as_view(), name='opportunity_source_edit'),
    path('sources/<uuid:pk>/delete/', views.OpportunitySourceDeleteView.as_view(), name='opportunity_source_delete'),
    path('sources/<uuid:pk>/scan/', views.OpportunitySourceScanNowView.as_view(), name='opportunity_source_scan'),

    path('matches/create/', views.OpportunityMatchCreateView.as_view(), name='opportunity_match_create'),
    path('matches/<uuid:pk>/', views.OpportunityMatchDetailView.as_view(), name='opportunity_match_detail'),
    path('matches/<uuid:pk>/edit/', views.OpportunityMatchUpdateView.as_view(), name='opportunity_match_edit'),
    path('matches/<uuid:pk>/delete/', views.OpportunityMatchDeleteView.as_view(), name='opportunity_match_delete'),
    path('matches/<uuid:pk>/mark-read/', views.OpportunityMatchMarkReadView.as_view(), name='opportunity_match_mark_read'),
    path('matches/<uuid:pk>/create-task/', views.CreateTaskFromOpportunityView.as_view(), name='opportunity_create_task'),
]