"""
apps/marketing/urls.py
======================

Wire into the project urls.py with:

    path('marketing/', include('apps.marketing.urls')),
"""
from django.urls import path
from apps.marketing import views

urlpatterns = [
    # ── Dashboard (CAC / LTV analytics) ──────────────────────────────────────
    path('', views.MarketingDashboardView.as_view(),
         name='marketing_dashboard'),
    path('data/', views.MarketingDashboardDataAPI.as_view(),
         name='marketing_dashboard_data'),

    # ── Campaigns ────────────────────────────────────────────────────────────
    path('campaigns/', views.CampaignListView.as_view(),
         name='marketing_campaign_list'),
    path('campaigns/new/', views.CampaignCreateView.as_view(),
         name='marketing_campaign_create'),
    path('campaigns/<uuid:pk>/', views.CampaignDetailView.as_view(),
         name='marketing_campaign_detail'),
    path('campaigns/<uuid:pk>/edit/', views.CampaignUpdateView.as_view(),
         name='marketing_campaign_update'),
    path('campaigns/<uuid:pk>/delete/', views.CampaignDeleteView.as_view(),
         name='marketing_campaign_delete'),

    # Campaign spend
    path('campaigns/<uuid:pk>/expenses/add/',
         views.CampaignExpenseAddView.as_view(),
         name='marketing_expense_add'),
    path('campaigns/<uuid:pk>/expenses/<uuid:exp_pk>/delete/',
         views.CampaignExpenseDeleteView.as_view(),
         name='marketing_expense_delete'),

    # ── Contacts (lead pipeline) ─────────────────────────────────────────────
    path('contacts/', views.ContactListView.as_view(),
         name='marketing_contact_list'),
    path('contacts/new/', views.ContactCreateView.as_view(),
         name='marketing_contact_create'),
    path('contacts/<uuid:pk>/', views.ContactDetailView.as_view(),
         name='marketing_contact_detail'),
    path('contacts/<uuid:pk>/edit/', views.ContactUpdateView.as_view(),
         name='marketing_contact_update'),
    path('contacts/<uuid:pk>/stage/', views.ContactStageView.as_view(),
         name='marketing_contact_stage'),
    path('contacts/<uuid:pk>/activity/',
         views.ContactActivityAddView.as_view(),
         name='marketing_contact_activity'),
    path('contacts/<uuid:pk>/delete/', views.ContactDeleteView.as_view(),
         name='marketing_contact_delete'),
]
