from django.urls import path
from apps.files import views

urlpatterns = [
    path('',                                    views.FileManagerView.as_view(),                name='file_manager'),
    path('upload/',                             views.FileUploadView.as_view(),                 name='file_upload'),
    path('convert/',                            views.ConvertToPDFPageView.as_view(),           name='convert_pdf_page'),
    path('my-signatures/',                      views.MySignaturesView.as_view(),               name='my_signatures'),
    path('saved-signatures/',                   views.SavedSignaturesView.as_view(),            name='saved_signatures'),
    path('saved-signatures/api/',               views.SavedSignatureAPIView.as_view(),          name='saved_signatures_api'),

    path('<uuid:pk>/download/',                 views.FileDownloadView.as_view(),               name='file_download'),
    path('<uuid:pk>/delete/',                   views.FileDeleteView.as_view(),                 name='file_delete'),
    path('<uuid:pk>/share/',                    views.FileShareView.as_view(),                  name='file_share'),
    path('<uuid:pk>/convert-pdf/',              views.ConvertToPDFView.as_view(),               name='file_convert_pdf'),
    path('<uuid:pk>/convert-for-signing/',      views.ConvertForSigningView.as_view(),          name='convert_for_signing'),
    path('<uuid:pk>/request-signature/',        views.SignatureRequestCreateView.as_view(),     name='create_signature_request'),

    path('folder/create/',                      views.FolderCreateView.as_view(),               name='folder_create'),
    path('folder/<uuid:pk>/delete/',            views.FolderDeleteView.as_view(),               name='folder_delete'),
    path('folder/<uuid:pk>/share/',             views.FolderShareView.as_view(),                name='folder_share'),

    path('signatures/new/',                     views.SignatureRequestCreateView.as_view(),     name='signature_request_new'),
    path('signatures/<uuid:pk>/',               views.SignatureRequestDetailView.as_view(),     name='signature_request_detail'),
    path('signatures/<uuid:pk>/fields/',        views.SaveSignatureFieldsView.as_view(),        name='save_signature_fields'),

    path('sign/<uuid:token>/',                  views.SignDocumentView.as_view(),               name='sign_document'),
    path('sign/<uuid:token>/preview/',          views.SignDocumentPreviewView.as_view(),        name='sign_document_preview'),
    path('sign/<uuid:token>/download/',         views.SignDocumentDownloadView.as_view(),       name='sign_document_download'),
    path('sign/<uuid:token>/field/<uuid:field_id>/', views.FillSignatureFieldView.as_view(),   name='fill_signature_field'),
]