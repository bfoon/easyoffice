from django.urls import path
from apps.files import views

urlpatterns = [
    path('', views.FileManagerView.as_view(), name='file_manager'),
    path('upload/', views.FileUploadView.as_view(), name='file_upload'),
    path('<uuid:pk>/download/', views.FileDownloadView.as_view(), name='file_download'),
    path('<uuid:pk>/delete/', views.FileDeleteView.as_view(), name='file_delete'),
    path('<uuid:pk>/share/', views.FileShareView.as_view(), name='file_share'),
    path('folder/create/', views.FolderCreateView.as_view(), name='folder_create'),
    path('folder/<uuid:pk>/delete/', views.FolderDeleteView.as_view(), name='folder_delete'),
    path('folder/<uuid:pk>/share/', views.FolderShareView.as_view(), name='folder_share'),
]