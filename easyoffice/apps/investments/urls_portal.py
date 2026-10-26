from django.urls import path

from apps.investments import views_portal as v

app_name = 'invest'

urlpatterns = [
    # Public (token links)
    path('apply/<str:token>/', v.ApplyView.as_view(), name='apply'),
    path('apply/<str:token>/done/', v.ApplyDoneView.as_view(), name='apply_done'),
    path('activate/<str:token>/', v.ActivateView.as_view(), name='activate'),

    # Investor portal
    path('', v.DashboardView.as_view(), name='dashboard'),
    path('listings/', v.OpportunityListView.as_view(), name='opportunities'),
    path('listings/<slug:slug>/', v.OpportunityDetailView.as_view(), name='opportunity_detail'),
    path('holdings/<uuid:pk>/', v.HoldingDetailView.as_view(), name='holding_detail'),
    path('wallet/', v.WalletView.as_view(), name='wallet'),
    path('wallet/statement.csv', v.StatementView.as_view(), name='statement'),
    path('profile/', v.ProfileView.as_view(), name='profile'),
]
