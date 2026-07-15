"""
apps/customforms/urls.py

Mount in the project urls.py:

    path('forms/', include('apps.customforms.urls')),
"""
from django.urls import path

from . import views

urlpatterns = [
    # ── Pages ────────────────────────────────────────────────────────────
    path('',                                    views.FormsHomeView.as_view(),            name='forms_home'),
    path('builder/',                            views.FormBuilderView.as_view(),          name='form_builder'),
    path('<uuid:pk>/fill/',                     views.FormFillView.as_view(),             name='form_fill'),
    path('<uuid:pk>/submit/',                   views.FormSubmitView.as_view(),           name='form_submit'),
    path('submissions/<uuid:pk>/',              views.SubmissionDetailView.as_view(),     name='form_submission_detail'),
    path('submissions/<uuid:pk>/steps/<uuid:step_id>/act/',
                                                views.SubmissionActionView.as_view(),     name='form_step_act'),

    # ── Template APIs (builder) ──────────────────────────────────────────
    path('api/templates/',                      views.FormTemplateListView.as_view(),     name='forms_api_list'),
    path('api/templates/<uuid:pk>/',            views.FormTemplateDetailView.as_view(),   name='forms_api_detail'),
    path('api/templates/<uuid:pk>/update/',     views.FormTemplateUpdateView.as_view(),   name='forms_api_update'),
    path('api/templates/<uuid:pk>/delete/',     views.FormTemplateDeleteView.as_view(),   name='forms_api_delete'),
    path('api/templates/<uuid:pk>/publish/',    views.FormTemplatePublishView.as_view(),  name='forms_api_publish'),
    path('api/templates/<uuid:pk>/duplicate/',  views.FormTemplateDuplicateView.as_view(),name='forms_api_duplicate'),
    path('api/directory/',                      views.DirectoryView.as_view(),            name='forms_api_directory'),

    # ── Exports ──────────────────────────────────────────────────────────
    # Empty printable form (pdf | docx)
    path('api/templates/<uuid:pk>/export/<str:fmt>/',
                                                views.FormTemplateExportView.as_view(),   name='forms_api_export'),
    # Filled submission (pdf | docx)
    path('submissions/<uuid:pk>/export/<str:fmt>/',
                                                views.SubmissionExportView.as_view(),     name='form_submission_export'),
]
