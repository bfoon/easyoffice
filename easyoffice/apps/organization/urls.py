from django.urls import path
from apps.organization import views

app_name = 'organization'

urlpatterns = [
    path('', views.OrgChartView.as_view(), name='org_chart'),
    path('departments/', views.DepartmentListView.as_view(), name='department_list'),
    path('units/', views.UnitListView.as_view(), name='unit_list'),
    path('save-layout/', views.SaveOrgChartLayoutView.as_view(), name='org_chart_save_layout'),
]