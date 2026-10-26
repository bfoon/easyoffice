from django.urls import path

from apps.investments import views_admin as v

app_name = 'investments'

urlpatterns = [
    path('', v.AdminDashboardView.as_view(), name='dashboard'),

    path('invitations/', v.InvitationListView.as_view(), name='invitation_list'),
    path('invitations/<uuid:pk>/', v.InvitationActionView.as_view(), name='invitation_action'),

    path('applications/', v.ApplicationListView.as_view(), name='application_list'),
    path('applications/<uuid:pk>/', v.ApplicationDetailView.as_view(), name='application_detail'),
    path('files/<str:kind>/<uuid:pk>/<str:field>/', v.PrivateFileView.as_view(), name='private_file'),

    path('investors/', v.InvestorListView.as_view(), name='investor_list'),
    path('investors/<uuid:pk>/', v.InvestorDetailView.as_view(), name='investor_detail'),

    path('deposits/', v.DepositListView.as_view(), name='deposit_list'),
    path('withdrawals/', v.WithdrawalListView.as_view(), name='withdrawal_list'),

    path('listings/', v.OpportunityListView.as_view(), name='opportunity_list'),
    path('listings/new/', v.OpportunityFormView.as_view(), name='opportunity_create'),
    path('listings/<uuid:pk>/', v.OpportunityDetailView.as_view(), name='opportunity_detail'),
    path('listings/<uuid:pk>/edit/', v.OpportunityFormView.as_view(), name='opportunity_edit'),

    path('distributions/', v.DistributionListView.as_view(), name='distribution_list'),
    path('distributions/<uuid:pk>/', v.DistributionDetailView.as_view(), name='distribution_detail'),
]
