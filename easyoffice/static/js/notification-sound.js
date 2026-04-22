/* ------------------------------------------------------------------
 * notification-sound.js
 * ------------------------------------------------------------------
 * Global polling that keeps these three things LIVE without reloading
 * the page:
 *
 *   1. Navbar unread badge         -> any element with [data-unread-badge]
 *   2. Per-room sidebar badges     -> [data-room-unread="<room_uuid>"]
 *   3. Notifications dropdown      -> [data-notifications-panel]
 *   4. Sound "ding" on DM/mention  -> Web Audio, 3 choices + volume
 *
 * Include once in base.html (inherited by every authenticated page):
 *
 *     {% if user.is_authenticated %}
 *         <script src="{% static 'js/notification-sound.js' %}" defer></script>
 *     {% endif %}
 *
 * Endpoint:  GET /messages/notifications/poll/?since=<iso8601>
 *
 * Preferences in localStorage:
 *     notify_enabled  -> "1" | "0"    (default "1")
 *     notify_sound    -> "ding" | "chime" | "pop"
 *     notify_volume   -> "0".."1"
 *
 * Public API:
 *     window.openNotifySoundSettings()  -> open preferences modal
 *     window.refreshNotifications()     -> force an immediate poll
 *     window.addEventListener('notifications:update', function(e){ ... })
 *         // e.detail = { total_unread, rooms_unread, recent }
 * ---------------------------------------------------------------- */
(function () {
    'use strict';

    if (window.__notifySoundLoaded) return;
    window.__notifySoundLoaded = true;

    // ------------------------------------------------------------------
    // Constants
    // ------------------------------------------------------------------
    var POLL_URL         = '/messages/notifications/poll/';
    var POLL_INTERVAL    = 8000;   // ms — foreground
    var POLL_INTERVAL_BG = 20000;  // ms — tab hidden

    var LS_ENABLED = 'notify_enabled';
    var LS_SOUND   = 'notify_sound';
    var LS_VOLUME  = 'notify_volume';
    var LS_SINCE   = 'notify_last_since';

    // ------------------------------------------------------------------
    // Preferences
    // ------------------------------------------------------------------
    function prefEnabled() {
        var v = localStorage.getItem(LS_ENABLED);
        return v === null ? true : v === '1';
    }
    function prefSound() {
        var v = localStorage.getItem(LS_SOUND);
        return (v === 'chime' || v === 'pop' || v === 'ding') ? v : 'ding';
    }
    function prefVolume() {
        var v = parseFloat(localStorage.getItem(LS_VOLUME));
        if (isNaN(v) || v < 0 || v > 1) return 0.6;
        return v;
    }

    // ------------------------------------------------------------------
    // Web Audio — synthesized sounds (no file assets)
    // ------------------------------------------------------------------
    var audioCtx = null;
    function getCtx() {
        if (audioCtx) return audioCtx;
        try {
            var Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) return null;
            audioCtx = new Ctx();
        } catch (e) { return null; }
        return audioCtx;
    }
    function beep(ctx, freq, startAt, durationSec, volume, type) {
        var osc = ctx.createOscillator();
        var gain = ctx.createGain();
        osc.type = type || 'sine';
        osc.frequency.setValueAtTime(freq, startAt);
        gain.gain.setValueAtTime(0.0001, startAt);
        gain.gain.exponentialRampToValueAtTime(volume, startAt + 0.015);
        gain.gain.exponentialRampToValueAtTime(0.0001, startAt + durationSec);
        osc.connect(gain).connect(ctx.destination);
        osc.start(startAt);
        osc.stop(startAt + durationSec + 0.02);
    }
    function playDing() {
        var ctx = getCtx(); if (!ctx) return;
        if (ctx.state === 'suspended') ctx.resume();
        var v = prefVolume(), t = ctx.currentTime;
        beep(ctx, 880,   t,         0.18, v,       'sine');
        beep(ctx, 1318.5, t + 0.09, 0.22, v * 0.9, 'sine');
    }
    function playChime() {
        var ctx = getCtx(); if (!ctx) return;
        if (ctx.state === 'suspended') ctx.resume();
        var v = prefVolume(), t = ctx.currentTime;
        beep(ctx, 523.25, t,        0.18, v, 'triangle');
        beep(ctx, 659.25, t + 0.10, 0.18, v, 'triangle');
        beep(ctx, 783.99, t + 0.20, 0.30, v, 'triangle');
    }
    function playPop() {
        var ctx = getCtx(); if (!ctx) return;
        if (ctx.state === 'suspended') ctx.resume();
        var v = prefVolume(), t = ctx.currentTime;
        var osc = ctx.createOscillator(), gain = ctx.createGain();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(1200, t);
        osc.frequency.exponentialRampToValueAtTime(400, t + 0.12);
        gain.gain.setValueAtTime(0.0001, t);
        gain.gain.exponentialRampToValueAtTime(v, t + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.14);
        osc.connect(gain).connect(ctx.destination);
        osc.start(t);
        osc.stop(t + 0.16);
    }
    function playSound(name) {
        try {
            if (name === 'chime')    playChime();
            else if (name === 'pop') playPop();
            else                     playDing();
        } catch (e) {}
    }

    // Unlock AudioContext on first user gesture (browser autoplay policy)
    function unlockAudioOnce() {
        var ctx = getCtx();
        if (ctx && ctx.state === 'suspended') ctx.resume();
        window.removeEventListener('click',      unlockAudioOnce, true);
        window.removeEventListener('keydown',    unlockAudioOnce, true);
        window.removeEventListener('touchstart', unlockAudioOnce, true);
    }
    window.addEventListener('click',      unlockAudioOnce, true);
    window.addEventListener('keydown',    unlockAudioOnce, true);
    window.addEventListener('touchstart', unlockAudioOnce, true);

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------
    function currentRoomId() {
        var meta = document.querySelector('meta[name="chat-room-id"]');
        if (meta && meta.content) return meta.content.trim();
        var m = location.pathname.match(
            /\/messages\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\//i
        );
        return m ? m[1] : null;
    }
    function tabVisible() { return !document.hidden; }

    function escapeHtml(s) {
        return String(s || '').replace(/[&<>"']/g, function (c) {
            return ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' })[c];
        });
    }

    function formatRelative(iso) {
        var d = new Date(iso);
        if (isNaN(d)) return '';
        var diff = (Date.now() - d.getTime()) / 1000; // seconds
        if (diff < 60)     return 'just now';
        if (diff < 3600)   return Math.floor(diff / 60) + 'm ago';
        if (diff < 86400)  return Math.floor(diff / 3600) + 'h ago';
        if (diff < 604800) return Math.floor(diff / 86400) + 'd ago';
        return d.toLocaleDateString();
    }

    // ------------------------------------------------------------------
    // LIVE DOM PATCHING — badges
    // ------------------------------------------------------------------
    function renderBadge(el, count) {
        if (!el) return;
        if (count > 0) {
            var txt = count > 99 ? '99+' : String(count);
            if (el.textContent !== txt) el.textContent = txt;
            el.style.display = '';
            el.removeAttribute('hidden');
            el.classList.remove('hidden');
        } else {
            el.textContent = '';
            el.style.display = 'none';
            el.setAttribute('hidden', '');
            el.classList.add('hidden');
        }
    }

    function updateBadges(total, perRoom) {
        // Navbar total badge(s)
        document.querySelectorAll('[data-unread-badge]').forEach(function (el) {
            renderBadge(el, total);
        });

        // Per-room sidebar badges — reset any that are no longer in the
        // payload, then set the ones that are.
        document.querySelectorAll('[data-room-unread]').forEach(function (el) {
            var rid = el.getAttribute('data-room-unread');
            var cnt = perRoom && perRoom[rid] ? perRoom[rid] : 0;
            renderBadge(el, cnt);
        });

        // Page title prefix, e.g. "(3) Dashboard — MyApp"
        var baseTitle = document.body.dataset.baseTitle;
        if (!baseTitle) {
            baseTitle = document.title.replace(/^\(\d+\)\s*/, '');
            document.body.dataset.baseTitle = baseTitle;
        }
        document.title = total > 0 ? '(' + (total > 99 ? '99+' : total) + ') ' + baseTitle
                                   : baseTitle;

        // Favicon dot (optional — only if the page has <link id="favicon">)
        updateFavicon(total > 0);
    }

    // ------------------------------------------------------------------
    // Favicon dot
    // ------------------------------------------------------------------
    var _origFavicon = null;
    function updateFavicon(hasUnread) {
        var link = document.getElementById('favicon')
                || document.querySelector('link[rel="icon"]');
        if (!link) return;
        if (!_origFavicon) _origFavicon = link.href;

        if (!hasUnread) {
            if (link.href !== _origFavicon) link.href = _origFavicon;
            return;
        }

        // Draw original icon + red dot on a canvas, swap href
        var img = new Image();
        img.crossOrigin = 'anonymous';
        img.onload = function () {
            try {
                var c = document.createElement('canvas');
                c.width = 32; c.height = 32;
                var ctx = c.getContext('2d');
                ctx.drawImage(img, 0, 0, 32, 32);
                ctx.fillStyle = '#ef4444';
                ctx.beginPath();
                ctx.arc(24, 8, 7, 0, 2 * Math.PI);
                ctx.fill();
                ctx.strokeStyle = '#fff';
                ctx.lineWidth = 2;
                ctx.stroke();
                link.href = c.toDataURL('image/png');
            } catch (e) { /* CORS etc. — silently ignore */ }
        };
        img.onerror = function () {};
        img.src = _origFavicon;
    }

    // ------------------------------------------------------------------
    // LIVE DOM PATCHING — notifications dropdown panel
    //
    // Any element with [data-notifications-panel] will be populated with a
    // list of recent DM/mention messages. The script injects its own styles.
    // ------------------------------------------------------------------
    function renderDropdown(recent) {
        var panels = document.querySelectorAll('[data-notifications-panel]');
        if (!panels.length) return;

        var html = '';
        if (!recent.length) {
            html = '<div class="ns-empty">No new messages</div>';
        } else {
            html = '<ul class="ns-list">';
            recent.forEach(function (n) {
                var icon = n.kind === 'mention' ? '@' : '💬';
                var unreadCls = n.is_unread ? ' ns-item-unread' : '';
                html += '<li class="ns-item' + unreadCls + '">'
                     +   '<a href="/messages/' + encodeURIComponent(n.room_id) + '/" class="ns-link">'
                     +     '<span class="ns-icon">' + icon + '</span>'
                     +     '<span class="ns-body">'
                     +       '<span class="ns-head">'
                     +         '<strong>' + escapeHtml(n.sender_name) + '</strong>'
                     +         '<span class="ns-meta">' + escapeHtml(n.room_name) + ' · '
                     +         escapeHtml(formatRelative(n.created_at)) + '</span>'
                     +       '</span>'
                     +       '<span class="ns-preview">' + escapeHtml(n.preview) + '</span>'
                     +     '</span>'
                     +   '</a>'
                     + '</li>';
            });
            html += '</ul>';
        }

        panels.forEach(function (p) {
            // Only replace if content changed, to avoid flicker/scroll reset
            if (p.__lastHtml !== html) {
                p.innerHTML = html;
                p.__lastHtml = html;
            }
        });
    }

    function injectDropdownStyles() {
        if (document.getElementById('ns-dropdown-styles')) return;
        var s = document.createElement('style');
        s.id = 'ns-dropdown-styles';
        s.textContent = [
            '[data-notifications-panel]{font-family:system-ui,-apple-system,sans-serif}',
            '.ns-list{list-style:none;margin:0;padding:0;max-height:420px;overflow-y:auto}',
            '.ns-item{border-bottom:1px solid #eee}',
            '.ns-item:last-child{border-bottom:0}',
            '.ns-link{display:flex;gap:10px;padding:12px 14px;text-decoration:none;',
              'color:#222;align-items:flex-start;transition:background .15s}',
            '.ns-link:hover{background:#f5f7fa}',
            '.ns-item-unread .ns-link{background:#eff6ff}',
            '.ns-item-unread .ns-link:hover{background:#dbeafe}',
            '.ns-icon{flex:0 0 auto;width:28px;height:28px;border-radius:50%;',
              'background:#e5e7eb;display:flex;align-items:center;justify-content:center;',
              'font-size:14px;font-weight:600}',
            '.ns-item-unread .ns-icon{background:#2563eb;color:#fff}',
            '.ns-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}',
            '.ns-head{display:flex;justify-content:space-between;gap:8px;align-items:baseline}',
            '.ns-head strong{font-size:14px;color:#111}',
            '.ns-meta{font-size:11px;color:#6b7280;white-space:nowrap;overflow:hidden;',
              'text-overflow:ellipsis;max-width:55%}',
            '.ns-preview{font-size:13px;color:#4b5563;overflow:hidden;',
              'text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;',
              '-webkit-box-orient:vertical}',
            '.ns-empty{padding:24px 14px;text-align:center;color:#9ca3af;font-size:14px}'
        ].join('');
        document.head.appendChild(s);
    }
    injectDropdownStyles();

    // ------------------------------------------------------------------
    // ------------------------------------------------------------------
    // IN-PAGE TOAST PILL
    // Small avatar+name+preview pill in the bottom-right corner when a
    // DM or @-mention arrives and the user isn't viewing that room.
    // Click = jump to that room. Auto-dismisses after 6s.
    // ------------------------------------------------------------------
    var TOAST_CONTAINER_ID = 'ns-toast-stack';
    var TOAST_DISMISS_MS   = 6000;
    var MAX_TOASTS         = 3;

    function injectToastStyles() {
        if (document.getElementById('ns-toast-styles')) return;
        var s = document.createElement('style');
        s.id = 'ns-toast-styles';
        s.textContent = [
            '#' + TOAST_CONTAINER_ID + '{',
              'position:fixed;right:18px;bottom:18px;z-index:100000;',
              'display:flex;flex-direction:column-reverse;gap:10px;',
              'pointer-events:none;font-family:system-ui,-apple-system,sans-serif;',
              'max-width:360px;',
            '}',
            '.ns-toast{',
              'pointer-events:auto;display:flex;align-items:center;gap:12px;',
              'background:#fff;color:#111;padding:10px 14px 10px 10px;',
              'border-radius:999px;box-shadow:0 10px 28px rgba(0,0,0,.18);',
              'border:1px solid rgba(0,0,0,.06);cursor:pointer;',
              'transform:translateY(20px) scale(.96);opacity:0;',
              'transition:transform .28s cubic-bezier(.2,.9,.25,1.15),opacity .22s;',
              'min-width:260px;max-width:360px;',
            '}',
            '.ns-toast.show{ transform:translateY(0) scale(1); opacity:1; }',
            '.ns-toast:hover{ box-shadow:0 12px 34px rgba(0,0,0,.23); }',
            '.ns-toast-avatar{',
              'flex:0 0 auto;width:40px;height:40px;border-radius:50%;',
              'background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;',
              'display:flex;align-items:center;justify-content:center;',
              'font-weight:700;font-size:.78rem;overflow:hidden;position:relative;',
            '}',
            '.ns-toast-avatar img{ width:100%;height:100%;object-fit:cover; }',
            '.ns-toast-kind{',
              'position:absolute;bottom:-2px;right:-2px;width:18px;height:18px;',
              'border-radius:50%;background:#fff;color:#2563eb;',
              'display:flex;align-items:center;justify-content:center;',
              'font-size:9px;font-weight:800;border:2px solid #fff;',
              'box-shadow:0 1px 3px rgba(0,0,0,.25);',
            '}',
            '.ns-toast-kind.mention{ background:#ef4444;color:#fff; }',
            '.ns-toast-body{ flex:1;min-width:0;padding-right:4px; }',
            '.ns-toast-title{',
              'font-size:.82rem;font-weight:600;color:#111;',
              'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;',
              'display:flex;align-items:center;gap:6px;',
            '}',
            '.ns-toast-badge{',
              'display:inline-block;font-size:.58rem;font-weight:700;',
              'padding:1px 6px;border-radius:8px;background:#eef2ff;color:#4338ca;',
              'text-transform:uppercase;letter-spacing:.03em;flex-shrink:0;',
            '}',
            '.ns-toast-badge.mention{ background:#fef2f2;color:#b91c1c; }',
            '.ns-toast-preview{',
              'font-size:.74rem;color:#4b5563;margin-top:1px;',
              'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;',
            '}',
            '.ns-toast-close{',
              'flex:0 0 auto;width:22px;height:22px;border-radius:50%;',
              'background:transparent;border:0;color:#9ca3af;cursor:pointer;',
              'display:flex;align-items:center;justify-content:center;',
              'font-size:14px;transition:background .1s,color .1s;',
            '}',
            '.ns-toast-close:hover{ background:#f3f4f6;color:#111; }',
            '@media (prefers-color-scheme: dark){',
              '.ns-toast{ background:#1f2937;color:#f3f4f6;border-color:rgba(255,255,255,.08); }',
              '.ns-toast-title{ color:#f3f4f6; }',
              '.ns-toast-preview{ color:#9ca3af; }',
              '.ns-toast-kind{ background:#1f2937; }',
              '.ns-toast-badge{ background:#312e81;color:#c7d2fe; }',
              '.ns-toast-badge.mention{ background:#7f1d1d;color:#fecaca; }',
              '.ns-toast-close:hover{ background:#374151;color:#f9fafb; }',
            '}',
            '@media (max-width:480px){',
              '#' + TOAST_CONTAINER_ID + '{ right:10px;bottom:10px;left:10px;max-width:none; }',
              '.ns-toast{ min-width:0; }',
            '}'
        ].join('');
        document.head.appendChild(s);
    }

    function getToastStack() {
        injectToastStyles();
        var stack = document.getElementById(TOAST_CONTAINER_ID);
        if (!stack) {
            stack = document.createElement('div');
            stack.id = TOAST_CONTAINER_ID;
            document.body.appendChild(stack);
        }
        return stack;
    }

    function escText(s) {
        return String(s || '').replace(/[&<>"']/g, function(c){
            return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
        });
    }

    function showToastPill(n) {
        try {
            var stack = getToastStack();

            // De-dupe by message id — if already showing, skip.
            if (stack.querySelector('[data-msg-id="' + n.id + '"]')) return;

            // Trim old toasts so we never stack more than MAX_TOASTS
            var existing = stack.querySelectorAll('.ns-toast');
            for (var i = 0; i <= existing.length - MAX_TOASTS; i++) {
                try { existing[i].remove(); } catch (_) {}
            }

            var toast = document.createElement('div');
            toast.className = 'ns-toast';
            toast.setAttribute('data-msg-id', n.id || '');
            toast.setAttribute('role', 'alert');
            toast.setAttribute('aria-live', 'polite');

            var isMention = n.kind === 'mention';
            var avatarInner = (n.sender_avatar_url)
                ? '<img src="' + escText(n.sender_avatar_url) + '" alt="">'
                : escText(n.sender_initials || (n.sender_name || '?').charAt(0).toUpperCase());
            var kindBadge = isMention ? '@' : '💬';
            var kindClass = isMention ? ' mention' : '';

            toast.innerHTML =
                '<div class="ns-toast-avatar">' +
                    avatarInner +
                    '<span class="ns-toast-kind' + kindClass + '">' + kindBadge + '</span>' +
                '</div>' +
                '<div class="ns-toast-body">' +
                    '<div class="ns-toast-title">' +
                        '<span style="overflow:hidden;text-overflow:ellipsis">' + escText(n.sender_name || 'New message') + '</span>' +
                        (isMention
                            ? '<span class="ns-toast-badge mention">mentioned you</span>'
                            : (n.room_type && n.room_type !== 'direct'
                                ? '<span class="ns-toast-badge">' + escText(n.room_name || '') + '</span>'
                                : '')) +
                    '</div>' +
                    '<div class="ns-toast-preview">' + escText(n.preview || '') + '</div>' +
                '</div>' +
                '<button type="button" class="ns-toast-close" aria-label="Dismiss">&times;</button>';

            // Click body → open room
            toast.addEventListener('click', function(e) {
                if (e.target.closest('.ns-toast-close')) return;
                try {
                    window.location.href = '/messages/' + n.room_id + '/';
                } catch (_) {}
            });
            // Click × → dismiss only
            var closeBtn = toast.querySelector('.ns-toast-close');
            closeBtn.addEventListener('click', function(e) {
                e.stopPropagation();
                dismissToast(toast);
            });

            stack.appendChild(toast);
            // Trigger the entrance animation on next frame
            requestAnimationFrame(function(){ toast.classList.add('show'); });

            // Auto-dismiss
            var dismissTimer = setTimeout(function(){ dismissToast(toast); }, TOAST_DISMISS_MS);

            // Pause auto-dismiss on hover
            toast.addEventListener('mouseenter', function(){
                if (dismissTimer) { clearTimeout(dismissTimer); dismissTimer = null; }
            });
            toast.addEventListener('mouseleave', function(){
                dismissTimer = setTimeout(function(){ dismissToast(toast); }, TOAST_DISMISS_MS);
            });
        } catch (e) {
            // no-op — toast failure should never break polling
        }
    }

    function dismissToast(toast) {
        if (!toast || !toast.parentNode) return;
        toast.classList.remove('show');
        setTimeout(function(){
            try { toast.remove(); } catch (_) {}
        }, 260);
    }

    // ------------------------------------------------------------------
    // NATIVE DESKTOP NOTIFICATION
    // Shown ONLY when the tab is hidden/not focused — the toast pill is
    // enough while the user is looking at the page. When the tab is in
    // the background, the OS-level notification is what actually gets
    // the user's attention.
    //
    // If several messages arrive at once we consolidate them into a
    // single notification rather than spamming.
    // ------------------------------------------------------------------
    function canUseDesktopNotif() {
        try {
            return ('Notification' in window) &&
                   Notification.permission === 'granted';
        } catch (e) { return false; }
    }

    function tabInForeground() {
        try {
            // Truly "in front of the user" needs BOTH: tab visible AND window focused.
            return !document.hidden && document.hasFocus();
        } catch (e) {
            return !document.hidden;
        }
    }

    // Request permission on first page load if not yet decided. Silent: we
    // never nag the user — one polite ask, then honor whatever they pick.
    function maybeRequestPermission() {
        try {
            if (!('Notification' in window)) return;
            if (Notification.permission !== 'default') return;
            // Don't pop the permission dialog until the user has clicked
            // somewhere — browsers ignore it before a user gesture anyway,
            // and it feels less ambushy.
            var asked = false;
            var askOnce = function () {
                if (asked) return;
                asked = true;
                try { Notification.requestPermission(); } catch (e) {}
                window.removeEventListener('click', askOnce, true);
                window.removeEventListener('keydown', askOnce, true);
            };
            window.addEventListener('click', askOnce, true);
            window.addEventListener('keydown', askOnce, true);
        } catch (e) {}
    }
    maybeRequestPermission();

    /**
     * Show a single native OS notification for one or more new messages.
     * @param {Array} notifs  List of notification payload objects.
     */
    function showDesktopNotification(notifs) {
        if (!canUseDesktopNotif()) return;
        if (!notifs || !notifs.length) return;

        try {
            var title, body, tag, roomId;

            if (notifs.length === 1) {
                var n = notifs[0];
                title = (n.kind === 'mention')
                    ? n.sender_name + ' mentioned you'
                    : 'New message from ' + n.sender_name;
                // Include room name as context for group chats/mentions
                if (n.kind === 'mention' && n.room_type !== 'direct' && n.room_name) {
                    title += ' in ' + n.room_name;
                }
                body = n.preview || '';
                tag  = 'msg-' + n.room_id;           // collapses further msgs for same room
                roomId = n.room_id;
            } else {
                // Consolidate — one notification covering everything new.
                // Count distinct rooms for a friendlier summary.
                var rooms = {};
                notifs.forEach(function (n) { rooms[n.room_id] = n.sender_name; });
                var roomKeys = Object.keys(rooms);
                title = notifs.length + ' new messages';
                if (roomKeys.length === 1) {
                    title = notifs.length + ' new messages from ' + rooms[roomKeys[0]];
                    roomId = roomKeys[0];
                } else {
                    // Mixed senders — linking to the first is reasonable
                    roomId = notifs[0].room_id;
                }
                // Show the most recent preview for a taste of the content
                var last = notifs[notifs.length - 1];
                body = last.sender_name + ': ' + (last.preview || '');
                tag  = 'msg-multi';
            }

            var options = {
                body: body,
                tag: tag,
                silent: true,   // we play our own sound via Web Audio
                renotify: true  // re-alert even if same tag
            };

            // Use sender/room avatar as the icon when we can
            var icon = (notifs.length === 1)
                ? (notifs[0].sender_avatar_url || notifs[0].room_avatar_url || '')
                : '';
            if (icon) options.icon = icon;

            var notif = new Notification(title, options);
            notif.onclick = function () {
                try { window.focus(); } catch (e) {}
                try { window.location.href = '/messages/' + roomId + '/'; } catch (e) {}
                notif.close();
            };

            // Auto-close after ~8s so it doesn't sit in the notification
            // tray forever (some OSes already do this; belt-and-suspenders).
            setTimeout(function () {
                try { notif.close(); } catch (e) {}
            }, 8000);
        } catch (e) {
            // Notifications can throw on some mobile browsers / blocked states
            // — fail quietly, the toast pill is still doing its job.
        }
    }

    // Legacy name preserved so the poll loop keeps working.
    function showDesktopNotif(n) {
        showToastPill(n);
    }

    // ------------------------------------------------------------------
    // Polling loop
    // ------------------------------------------------------------------
    var pollTimer = null;
    var inFlight = false;

    function poll() {
        if (inFlight) { schedule(); return; }
        inFlight = true;

        var since = localStorage.getItem(LS_SINCE) || '';
        var url = POLL_URL + (since ? ('?since=' + encodeURIComponent(since)) : '');

        fetch(url, {
            credentials: 'same-origin',
            headers: { 'Accept': 'application/json' }
        })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
            if (!data || !data.ok) return;

            if (data.now) localStorage.setItem(LS_SINCE, data.now);

            // ── LIVE UPDATES (always, regardless of sound pref) ────────
            updateBadges(data.total_unread || 0, data.rooms_unread || {});
            renderDropdown(data.recent || []);

            // Broadcast event so other code on the page can hook in
            try {
                window.dispatchEvent(new CustomEvent('notifications:update', {
                    detail: {
                        total_unread: data.total_unread || 0,
                        rooms_unread: data.rooms_unread || {},
                        recent:       data.recent || []
                    }
                }));
            } catch (e) {}

            // ── SOUND + TOAST + DESKTOP (only if enabled & NEW items) ──
            if (!prefEnabled()) return;
            var notifs = data.notifications || [];
            if (!notifs.length) return;

            var viewingRoom = currentRoomId();
            var shouldRing = false;
            var toastCount = 0;
            var desktopCandidates = [];   // accumulate for the single OS notif
            var tabFocused = tabInForeground();

            for (var i = 0; i < notifs.length; i++) {
                var n = notifs[i];
                if (viewingRoom && n.room_id === viewingRoom && tabVisible()) {
                    continue; // already looking at it
                }
                shouldRing = true;

                // In-page pill toast — always (capped at 3 per cycle)
                if (toastCount < 3) {
                    showToastPill(n);
                    toastCount++;
                }

                // Desktop OS notif — only when tab is NOT focused,
                // otherwise the toast is enough and we'd be double-pinging.
                if (!tabFocused) {
                    desktopCandidates.push(n);
                }
            }

            // One consolidated desktop notification per poll cycle
            if (desktopCandidates.length) {
                showDesktopNotification(desktopCandidates);
            }
            if (shouldRing) playSound(prefSound());
        })
        .catch(function () {})
        .finally(function () {
            inFlight = false;
            schedule();
        });
    }

    function schedule() {
        if (pollTimer) clearTimeout(pollTimer);
        var delay = tabVisible() ? POLL_INTERVAL : POLL_INTERVAL_BG;
        pollTimer = setTimeout(poll, delay);
    }

    // Force an immediate refresh (e.g. after the user opens/closes a room)
    window.refreshNotifications = function () {
        if (pollTimer) clearTimeout(pollTimer);
        pollTimer = setTimeout(poll, 50);
    };

    // Faster cadence when tab regains focus
    document.addEventListener('visibilitychange', function () {
        if (tabVisible()) window.refreshNotifications();
    });

    // First poll — quick so badges populate right after page load
    setTimeout(poll, 500);

    // ------------------------------------------------------------------
    // Settings modal (preferences for sound)
    // ------------------------------------------------------------------
    function injectModalStyles() {
        if (document.getElementById('notify-sound-styles')) return;
        var s = document.createElement('style');
        s.id = 'notify-sound-styles';
        s.textContent = [
            '.ns-modal{position:fixed;inset:0;background:rgba(0,0,0,.45);',
              'display:flex;align-items:center;justify-content:center;z-index:99999;',
              'font-family:system-ui,-apple-system,sans-serif}',
            '.ns-card{background:#fff;border-radius:12px;padding:24px;',
              'width:min(92vw,420px);box-shadow:0 20px 60px rgba(0,0,0,.3)}',
            '.ns-card h3{margin:0 0 16px;font-size:18px;color:#111}',
            '.ns-row{display:flex;align-items:center;justify-content:space-between;',
              'margin:12px 0;color:#333;font-size:14px}',
            '.ns-row label{flex:1;cursor:pointer}',
            '.ns-row select,.ns-row input[type=range]{padding:6px 8px;',
              'border:1px solid #d0d0d0;border-radius:6px;font-size:14px}',
            '.ns-row input[type=range]{width:160px;padding:0}',
            '.ns-btn{padding:8px 14px;border:0;border-radius:8px;cursor:pointer;',
              'font-size:14px;font-weight:500}',
            '.ns-btn-primary{background:#2563eb;color:#fff}',
            '.ns-btn-ghost{background:#f1f1f1;color:#333}',
            '.ns-btn-test{background:#f1f1f1;color:#333;padding:4px 10px;font-size:12px;',
              'border-radius:6px;margin-left:8px}',
            '.ns-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:20px}',
            '.ns-toggle{width:44px;height:24px;background:#ccc;border-radius:12px;',
              'position:relative;cursor:pointer;transition:background .2s}',
            '.ns-toggle.on{background:#2563eb}',
            '.ns-toggle::after{content:"";position:absolute;top:2px;left:2px;',
              'width:20px;height:20px;background:#fff;border-radius:50%;transition:left .2s}',
            '.ns-toggle.on::after{left:22px}'
        ].join('');
        document.head.appendChild(s);
    }

    function openSettings() {
        injectModalStyles();
        var existing = document.getElementById('ns-modal-root');
        if (existing) existing.remove();

        var root = document.createElement('div');
        root.id = 'ns-modal-root';
        root.className = 'ns-modal';
        root.innerHTML = [
            '<div class="ns-card" role="dialog" aria-label="Notification sound settings">',
              '<h3>🔔 Message notifications</h3>',
              '<div class="ns-row">',
                '<label for="ns-enabled">Play a sound for DMs &amp; mentions</label>',
                '<div id="ns-enabled" class="ns-toggle" role="switch" tabindex="0"></div>',
              '</div>',
              '<div class="ns-row">',
                '<label for="ns-sound">Sound</label>',
                '<div>',
                  '<select id="ns-sound">',
                    '<option value="ding">Ding</option>',
                    '<option value="chime">Chime</option>',
                    '<option value="pop">Pop</option>',
                  '</select>',
                  '<button type="button" class="ns-btn ns-btn-test" id="ns-test">Test</button>',
                '</div>',
              '</div>',
              '<div class="ns-row">',
                '<label for="ns-volume">Volume</label>',
                '<input type="range" id="ns-volume" min="0" max="1" step="0.05">',
              '</div>',
              '<div class="ns-actions">',
                '<button type="button" class="ns-btn ns-btn-ghost" id="ns-close">Close</button>',
                '<button type="button" class="ns-btn ns-btn-primary" id="ns-save">Save</button>',
              '</div>',
            '</div>'
        ].join('');
        document.body.appendChild(root);

        var toggle  = root.querySelector('#ns-enabled');
        var soundEl = root.querySelector('#ns-sound');
        var volEl   = root.querySelector('#ns-volume');

        var enabled = prefEnabled();
        if (enabled) toggle.classList.add('on');
        soundEl.value = prefSound();
        volEl.value   = String(prefVolume());

        toggle.addEventListener('click', function () {
            enabled = !enabled;
            toggle.classList.toggle('on', enabled);
        });
        toggle.addEventListener('keydown', function (e) {
            if (e.key === ' ' || e.key === 'Enter') {
                e.preventDefault();
                enabled = !enabled;
                toggle.classList.toggle('on', enabled);
            }
        });

        root.querySelector('#ns-test').addEventListener('click', function () {
            var prevVol = localStorage.getItem(LS_VOLUME);
            localStorage.setItem(LS_VOLUME, String(volEl.value));
            playSound(soundEl.value);
            setTimeout(function () {
                if (prevVol === null) localStorage.removeItem(LS_VOLUME);
                else localStorage.setItem(LS_VOLUME, prevVol);
            }, 1500);
        });

        function close() { root.remove(); }
        root.querySelector('#ns-close').addEventListener('click', close);
        root.addEventListener('click', function (e) {
            if (e.target === root) close();
        });

        root.querySelector('#ns-save').addEventListener('click', function () {
            localStorage.setItem(LS_ENABLED, enabled ? '1' : '0');
            localStorage.setItem(LS_SOUND, soundEl.value);
            localStorage.setItem(LS_VOLUME, String(volEl.value));
            // If the user just enabled notifications and hasn't decided on
            // desktop permission yet, ask now (this IS a user gesture).
            if (enabled && 'Notification' in window &&
                Notification.permission === 'default') {
                try { Notification.requestPermission(); } catch (e) {}
            }
            close();
        });
    }

    window.openNotifySoundSettings = openSettings;
})();