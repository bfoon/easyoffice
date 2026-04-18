from django.urls import path
from . import views

urlpatterns = [
    path('',                          views.InvoiceDashboardView.as_view(),  name='invoice_dashboard'),
    path('new/',                      views.NewInvoiceView.as_view(),        name='invoice_new'),
    path('letterheads/',              views.LetterheadListView.as_view(),    name='invoice_letterheads'),

    # Templates
    path('templates/',                 views.TemplateListView.as_view(),              name='template_list'),
    path('templates/<uuid:pk>/use/',   views.TemplateUseView.as_view(),               name='template_use'),
    path('templates/<uuid:pk>/edit/',  views.TemplateEditView.as_view(),              name='template_edit'),
    path('templates/<uuid:pk>/delete/', views.TemplateDeleteView.as_view(),           name='template_delete'),
    path('<uuid:pk>/save-as-template/', views.TemplateCreateFromInvoiceView.as_view(), name='template_create_from_invoice'),

    path('<uuid:pk>/',                views.InvoiceDetailView.as_view(),     name='invoice_detail'),
    path('<uuid:pk>/edit/',           views.InvoiceBuilderView.as_view(),    name='invoice_edit'),
    path('<uuid:pk>/delete/',         views.InvoiceDeleteView.as_view(),     name='invoice_delete'),
    path('<uuid:pk>/void/',           views.InvoiceVoidView.as_view(),       name='invoice_void'),
    path('<uuid:pk>/duplicate/',      views.InvoiceDuplicateView.as_view(),  name='invoice_duplicate'),
    path('<uuid:pk>/convert/',        views.InvoiceConvertView.as_view(),    name='invoice_convert'),
    path('convertible-sources/',     views.ConvertibleSourcesListView.as_view(), name='invoice_convertible_sources'),

    path('<uuid:pk>/metadata/',       views.InvoiceMetadataSaveView.as_view(), name='invoice_metadata'),
    path('<uuid:pk>/items/',          views.InvoiceItemsSaveView.as_view(),    name='invoice_items'),
    path('<uuid:pk>/layout/',         views.InvoiceLayoutSaveView.as_view(),   name='invoice_layout'),
    path('<uuid:pk>/preview-pdf/',    views.InvoicePreviewPDFView.as_view(),   name='invoice_preview_pdf'),
    path('<uuid:pk>/finalize/',       views.InvoiceFinalizeView.as_view(),     name='invoice_finalize'),
]