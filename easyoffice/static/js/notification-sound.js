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
    // Desktop notification
    // ------------------------------------------------------------------
    function showDesktopNotif(n) {
        try {
            if (!('Notification' in window)) return;
            if (Notification.permission !== 'granted') return;
            var title = n.kind === 'mention'
                ? n.sender_name + ' mentioned you'
                : 'New message from ' + n.sender_name;
            var notif = new Notification(title, {
                body: n.preview || '',
                tag: 'msg-' + n.room_id,
                silent: true
            });
            notif.onclick = function () {
                window.focus();
                window.location.href = '/messages/' + n.room_id + '/';
                notif.close();
            };
        } catch (e) {}
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

            // ── SOUND (only if enabled & there are NEW items) ──────────
            if (!prefEnabled()) return;
            var notifs = data.notifications || [];
            if (!notifs.length) return;

            var viewingRoom = currentRoomId();
            var shouldRing = false;
            var firstToShow = null;

            for (var i = 0; i < notifs.length; i++) {
                var n = notifs[i];
                if (viewingRoom && n.room_id === viewingRoom && tabVisible()) {
                    continue; // already looking at it
                }
                shouldRing = true;
                if (!firstToShow) { firstToShow = n; showDesktopNotif(n); }
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
            if (enabled && 'Notification' in window &&
                Notification.permission === 'default') {
                try { Notification.requestPermission(); } catch (e) {}
            }
            close();
        });
    }

    window.openNotifySoundSettings = openSettings;
})();