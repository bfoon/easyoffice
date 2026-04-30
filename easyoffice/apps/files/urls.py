from django.urls import path, include
from apps.files import views

urlpatterns = [
    path('',                                    views.FileManagerView.as_view(),                name='file_manager'),
    path('upload/',                             views.FileUploadView.as_view(),                 name='file_upload'),
    path('convert/',                            views.ConvertToPDFPageView.as_view(),           name='convert_pdf_page'),
    path('my-signatures/',                      views.MySignaturesView.as_view(),               name='my_signatures'),
    path('saved-signatures/',                   views.SavedSignaturesView.as_view(),            name='saved_signatures'),
    path('saved-signatures/api/',               views.SavedSignatureAPIView.as_view(),          name='saved_signatures_api'),

    path('<uuid:pk>/download/',                 views.FileDownloadView.as_view(),               name='file_download'),
    path('<uuid:pk>/preview/',                  views.FilePreviewView.as_view(),                name='file_preview'),
    path('<uuid:pk>/preview-info/',             views.FilePreviewInfoView.as_view(),            name='file_preview_info'),
    path('<uuid:pk>/move/',                     views.FileMoveView.as_view(),                   name='file_move'),
    path('<uuid:pk>/rename/',                   views.FileRenameView.as_view(),                 name='file_rename'),
    path('<uuid:pk>/delete/',                   views.FileDeleteView.as_view(),                 name='file_delete'),
    path('<uuid:pk>/share/',                    views.FileShareView.as_view(),                  name='file_share'),
    path('<uuid:pk>/convert-pdf/',              views.ConvertToPDFView.as_view(),               name='file_convert_pdf'),
    path('<uuid:pk>/request-signature/',        views.SignatureRequestCreateView.as_view(),     name='create_signature_request'),

    path('folder/create/',                      views.FolderCreateView.as_view(),               name='folder_create'),
    path('folder/<uuid:pk>/delete/',            views.FolderDeleteView.as_view(),               name='folder_delete'),
    path('folder/<uuid:pk>/rename/',            views.FolderRenameView.as_view(),               name='folder_rename'),
    path('folder/<uuid:pk>/share/',             views.FolderShareView.as_view(),                name='folder_share'),
    path('recycle-bin/',                        views.RecycleBinView.as_view(),                  name='recycle_bin'),
    path('recycle-bin/<int:pk>/restore/',       views.RestoreTrashItemView.as_view(),           name='restore_trash_item'),
    path('history/<uuid:pk>/',                  views.FileHistoryView.as_view(),                name='file_history'),
    path('folder/<uuid:pk>/move/',              views.FolderMoveView.as_view(),                 name='folder_move'),

    path('signatures/new/',                     views.SignatureRequestCreateView.as_view(),     name='signature_request_new'),
    path('signatures/<uuid:pk>/',               views.SignatureRequestDetailView.as_view(),     name='signature_request_detail'),
    path('signatures/<uuid:pk>/fields/',        views.SaveSignatureFieldsView.as_view(),        name='save_signature_fields'),
    path('signatures/<uuid:pk>/creator-sign/',  views.CreatorSignView.as_view(),                name='creator_sign'),
    path(
        'signatures/<uuid:pk>/duplicate/',
        views.DuplicateSignatureRequestView.as_view(),
        name='signature_request_duplicate',
    ),
    path(
        'signatures/<uuid:pk>/signers/reorder/',
        views.SignerReorderView.as_view(),
        name='signature_request_signers_reorder',
    ),
    path(
        'signatures/<uuid:pk>/documents/',
        views.SignatureRequestDocumentManageView.as_view(),
        name='signature_request_documents_manage',
    ),

    path(
        '<uuid:pk>/convert-for-signing/',
        views.SignatureConvertForSigningView.as_view(),
        name='convert_for_signing'
    ),


    path('sign/<uuid:token>/',                  views.SignDocumentView.as_view(),               name='sign_document'),
    path('sign/<uuid:token>/preview/',          views.SignDocumentPreviewView.as_view(),        name='sign_document_preview'),
    path('sign/<uuid:token>/download/',         views.SignDocumentDownloadView.as_view(),       name='sign_document_download'),
    path('sign/<uuid:token>/field/<uuid:field_id>/', views.FillSignatureFieldView.as_view(),   name='fill_signature_field'),

    # Public no-login completion page after a signer signs or declines.
    # NOTE: This used to have two routes registered under the same name —
    # the first one pointed to SignatureCompleteView which renders a
    # template (sign_complete.html) that does not exist, producing a blank
    # page. We now use the SignDocumentCompleteView which renders the
    # proper sign_document_complete.html template.
    path(
        'sign/<uuid:token>/complete/',
        views.SignDocumentCompleteView.as_view(),
        name='sign_document_complete'
    ),

    path('pdf-tools/',                          views.PDFToolsPageView.as_view(),               name='pdf_tools_page'),
    path('pdf-tools/merge/',                    views.PDFMergeView.as_view(),                   name='pdf_merge'),
    path('pdf-tools/remove-pages/<uuid:pk>/',   views.PDFRemovePagesView.as_view(),             name='pdf_remove_pages'),
    path('pdf-tools/reorder-pages/<uuid:pk>/',  views.PDFReorderPagesView.as_view(),            name='pdf_reorder_pages'),
    path('pdf-tools/rotate-pages/<uuid:pk>/',   views.PDFRotatePagesView.as_view(),             name='pdf_rotate_pages'),
    path('pdf-tools/split/<uuid:pk>/',          views.PDFSplitView.as_view(),                   name='pdf_split'),
    path('pdf-tools/merge-images/',             views.PDFMergeImagesView.as_view(),             name='pdf_merge_images'),
    path('tools/zip-extract/',                  views.ZipExtractView.as_view(),                  name='zip_extract'),
    path('tools/letterhead-apply/',             views.LetterheadApplyToolView.as_view(),        name='letterhead_apply_tool'),

    path('quick-sign/',                         views.QuickSignView.as_view(),                  name='quick_sign'),
    path('quick-sign/<uuid:pk>/',               views.QuickSignView.as_view(),                  name='quick_sign_file'),
    path('quick-sign/<uuid:pk>/preview/', views.QuickSignPreviewView.as_view(), name='quick_sign_preview'),

    path('tools/notes/',                        views.NotesToPDFView.as_view(),                 name='notes_to_pdf'),
    path('tools/pdf-to-word/',                  views.PDFToWordView.as_view(),                  name='pdf_to_word'),
    path('tools/pdf-to-image/',                 views.PDFToImageView.as_view(),                 name='pdf_to_image'),

    path('pin/',                                views.PinToggleView.as_view(),                  name='pin_toggle'),
    path('public/<uuid:token>/',                views.FilePublicDownloadView.as_view(),         name='file_public_download'),
    path('signatures/view/<uuid:token>/',       views.SignatureViewOnlyView.as_view(),          name='signature_view_only'),

    path('<uuid:pk>/note/',                     views.FileNoteView.as_view(),                    name='file_note'),
    path('<uuid:pk>/note/share/',               views.FileNoteShareView.as_view(),               name='file_note_share'),
    path('<uuid:pk>/note/share/revoke_all/',    views.FileNoteShareView.as_view(),               name='file_note_share_revoke_all'),
    path('<uuid:pk>/note/share/<uuid:share_id>/', views.FileNoteShareView.as_view(),            name='file_note_share_detail'),
    path('<uuid:pk>/note/typing/',              views.FileNoteTypingView.as_view(),              name='file_note_typing'),
    path('note/bulk-status/',                   views.FileNoteBulkStatusView.as_view(),          name='file_note_bulk_status'),
    path('recycle-bin/<int:pk>/delete/',        views.PermanentDeleteTrashFileView.as_view(),   name='delete_trash_file'),
    path('<uuid:pk>/annotations/',              views.FileAnnotationView.as_view(),              name='file_annotations'),

    path('<uuid:pk>/convert-for-signing/', views.SignatureConvertForSigningView.as_view(), name='convert_for_signing'),


    # ── Live Preview Session ─────────────────────────────────────────────────
    path('<uuid:pk>/live-preview/start/',       views.LivePreviewStartView.as_view(),           name='live_preview_start'),
    path('<uuid:pk>/live-preview/status/',      views.LivePreviewStatusView.as_view(),          name='live_preview_status'),
    path('<uuid:pk>/live-preview/users/',       views.LivePreviewUsersView.as_view(),           name='live_preview_users'),
    path('live-preview/<uuid:token>/accept/',   views.LivePreviewAcceptView.as_view(),          name='live_preview_accept'),
    path('live-preview/<uuid:token>/end/',      views.LivePreviewEndView.as_view(),             name='live_preview_end'),

    path('collabora/', include('apps.files.collabora.urls')),
]