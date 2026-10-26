from django.urls import path

from apps.features import views

app_name = 'features'

urlpatterns = [
    path('', views.FeatureControlPanelView.as_view(), name='panel'),
    path('audit/', views.FeatureAuditLogView.as_view(), name='audit'),
    path('<str:key>/mode/', views.FeatureSetModeView.as_view(), name='set_mode'),
    path('<str:key>/settings/', views.FeatureSettingsView.as_view(), name='settings'),
]
