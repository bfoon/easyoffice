from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from apps.mobile_api import views
from apps.mobile_api import views_tasks
from apps.mobile_api import views_files_sign as fs
from apps.mobile_api import views_files_upload
from apps.mobile_api import views_file_bridge
from apps.mobile_api import views_pos

app_name = 'mobile_api'

urlpatterns = [
    # AUTH
    path('auth/login/',     views.MobileLoginView.as_view(),  name='login'),
    path('auth/refresh/',   TokenRefreshView.as_view(),       name='refresh'),
    path('auth/logout/',    views.MobileLogoutView.as_view(), name='logout'),
    path('auth/me/',        views.MobileMeView.as_view(),     name='me'),

    # ROOMS
    path('rooms/',                              views.RoomListView.as_view(),    name='rooms'),
    path('rooms/direct/',                       views.DirectRoomView.as_view(),  name='direct_room'),
    path('rooms/<uuid:room_id>/',               views.RoomDetailView.as_view(),  name='room_detail'),
    path('rooms/<uuid:room_id>/messages/',      views.RoomMessagesView.as_view(),name='room_messages'),
    path('rooms/<uuid:room_id>/polls/',         views.CreatePollView.as_view(),  name='create_poll'),

    # MESSAGES
    path('messages/<uuid:message_id>/',         views.MessageDeleteView.as_view(),   name='message_delete'),
    path('messages/<uuid:message_id>/react/',   views.ReactionToggleView.as_view(),  name='message_react'),
    path('rooms/<uuid:room_id>/upload/', views.RoomUploadView.as_view(), name='room_upload'),
    path('messages/<uuid:message_id>/edit/', views.MessageEditView.as_view(), name='message_edit'),
    path('messages/<uuid:message_id>/hide/', views.MessageHideView.as_view(), name='message_hide'),

    # TASKS
    path('tasks/',                          views_tasks.MyTasksView.as_view(),              name='my_tasks'),
    path('tasks/recent-closed/',            views_tasks.MyRecentClosedTasksView.as_view(),  name='my_recent_closed'),
    path('tasks/<uuid:task_id>/',           views_tasks.TaskDetailView.as_view(),           name='task_detail_api'),
    path('tasks/<uuid:task_id>/on-site/',   views_tasks.TaskOnSiteView.as_view(),           name='task_on_site_api'),
    path('tasks/<uuid:task_id>/clear-on-site/', views_tasks.TaskClearOnSiteView.as_view(),  name='task_clear_on_site_api'),
    path('tasks/<uuid:task_id>/complete/',  views_tasks.TaskCompleteView.as_view(),         name='task_complete_api'),

    # POLLS
    path('polls/<uuid:poll_id>/vote/',          views.VotePollView.as_view(),    name='poll_vote'),

    # PRESENCE
    path('presence/heartbeat/',                 views.PresenceHeartbeatView.as_view(), name='presence_hb'),
    path('presence/<uuid:user_id>/',            views.PresenceQueryView.as_view(),     name='presence'),

    # USERS
    path('users/search/',                       views.UserSearchView.as_view(),  name='user_search'),

    # DEVICE TOKENS
    path('device-tokens/',                      views.DeviceTokenView.as_view(), name='device_tokens'),

    # ── Files ────────────────────────────────────────────────────────────────
    path('files/', fs.MobileFileListView.as_view(), name='mobile_file_list'),
    path('files/<uuid:file_id>/', fs.MobileFileDetailView.as_view(), name='mobile_file_detail'),
    path('files/upload/', views_files_upload.MobileFileUploadView.as_view(),
         name='mobile_file_upload'),
    path('rooms/<uuid:room_id>/attach-file/',
         views_file_bridge.MobileAttachFileView.as_view(),
         name='mobile_attach_file'),
    path('rooms/<uuid:room_id>/messages/<uuid:message_id>/save-to-files/',
         views_file_bridge.MobileSaveToFilesView.as_view(),
         name='mobile_save_to_files'),

    # ── Signatures ───────────────────────────────────────────────────────────
    path('sign/requests/', fs.MobileSignRequestsView.as_view(), name='mobile_sign_requests'),
    path('sign/requests/<uuid:request_id>/', fs.MobileSignRequestDetailView.as_view(), name='mobile_sign_detail'),
    path('sign/requests/<uuid:request_id>/fields/<uuid:field_id>/', fs.MobileSignFieldView.as_view(),
         name='mobile_sign_field'),
    path('sign/requests/<uuid:request_id>/submit/', fs.MobileSignSubmitView.as_view(), name='mobile_sign_submit'),
    path('sign/requests/<uuid:request_id>/decline/', fs.MobileSignDeclineView.as_view(), name='mobile_sign_decline'),

    # ── Point of Sale (handheld till) ────────────────────────────────────────
    path('pos/lookup/',            views_pos.MobilePOSLookupView.as_view(),         name='mobile_pos_lookup'),
    path('pos/search/',            views_pos.MobilePOSSearchView.as_view(),         name='mobile_pos_search'),
    path('pos/basket/',            views_pos.MobilePOSBasketView.as_view(),         name='mobile_pos_basket'),
    path('pos/basket/add/',        views_pos.MobilePOSBasketAddView.as_view(),      name='mobile_pos_basket_add'),
    path('pos/basket/qty/',        views_pos.MobilePOSBasketQtyView.as_view(),      name='mobile_pos_basket_qty'),
    path('pos/basket/discount/',   views_pos.MobilePOSBasketDiscountView.as_view(), name='mobile_pos_basket_discount'),
    path('pos/basket/clear/',      views_pos.MobilePOSBasketClearView.as_view(),    name='mobile_pos_basket_clear'),
    path('pos/complete/',          views_pos.MobilePOSCompleteView.as_view(),       name='mobile_pos_complete'),
    path('pos/receipt/<uuid:sale_id>/email/',
         views_pos.MobilePOSReceiptEmailView.as_view(),                             name='mobile_pos_receipt_email'),
    path('pos/sales/today/',       views_pos.MobilePOSSalesTodayView.as_view(),     name='mobile_pos_sales_today'),
]
