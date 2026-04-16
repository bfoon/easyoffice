/**
 * collabora_presence.js
 * ─────────────────────
 * Renders a live "who's editing this file" avatar stack. Works in two modes:
 *
 *   1. Editor mode: the editor page sets window.CE_FILE_ID /
 *      window.CE_AVATARS_EL / window.CE_COUNT_EL before loading this
 *      script. We subscribe to presence for that one file and update
 *      the top bar.
 *
 *   2. Grid mode: the file manager grid contains any number of
 *      <span class="ce-card-presence" data-file-id="<uuid>"> elements.
 *      For each one, we open a WebSocket subscription and update the
 *      avatars as presence changes.
 *
 * WebSocket URL: /ws/files/<file_id>/presence/
 * Server sends: { "type": "presence", "editors": [{user_id, name, initials, permission, since}] }
 */
(function () {
  'use strict';

  function wsUrl(fileId) {
    var scheme = (window.location.protocol === 'https:') ? 'wss:' : 'ws:';
    return scheme + '//' + window.location.host + '/ws/files/' + fileId + '/presence/';
  }

  function makeAvatar(editor) {
    var el = document.createElement('span');
    el.className = 'ce-avatar';
    el.setAttribute('data-perm', editor.permission || 'view');
    el.setAttribute('data-user-id', editor.user_id);
    el.title = editor.name + ' (' + (editor.permission || 'view') + ')';
    el.textContent = editor.initials || '?';
    return el;
  }

  // Card-mode avatars are smaller and use inline style so we don't need
  // to touch the site's CSS.
  function makeCardAvatar(editor) {
    var el = document.createElement('span');
    var colorByPerm = {
      view: '#f59e0b',
      edit: '#3b82f6',
      full: '#10b981',
    };
    var bg = colorByPerm[editor.permission] || '#4f46e5';
    el.style.cssText = [
      'display:inline-flex', 'align-items:center', 'justify-content:center',
      'width:22px', 'height:22px', 'border-radius:50%',
      'background:' + bg, 'color:#fff',
      'font-size:.62rem', 'font-weight:700',
      'border:2px solid #fff',
      'margin-left:-6px',
    ].join(';');
    el.title = editor.name + ' (' + (editor.permission || 'view') + ')';
    el.textContent = editor.initials || '?';
    el.setAttribute('data-user-id', editor.user_id);
    return el;
  }

  function renderEditorMode(editors) {
    var wrap = window.CE_AVATARS_EL;
    var count = window.CE_COUNT_EL;
    if (!wrap) return;
    wrap.innerHTML = '';
    editors.forEach(function (e) { wrap.appendChild(makeAvatar(e)); });
    if (count) {
      count.textContent = editors.length + ' editor' + (editors.length === 1 ? '' : 's');
    }
  }

  function renderCard(container, editors) {
    var avatars = container.querySelector('.ce-card-avatars');
    var countEl = container.querySelector('.ce-card-count');
    if (!avatars) return;

    avatars.innerHTML = '';
    // Cap at 4 visible avatars for space; the count still reflects all.
    editors.slice(0, 4).forEach(function (e) {
      avatars.appendChild(makeCardAvatar(e));
    });
    if (countEl) countEl.textContent = editors.length;

    // Show/hide the whole widget depending on whether anyone's editing.
    container.style.display = editors.length > 0 ? 'inline-flex' : 'none';
  }

  // ── Reconnection with exponential backoff ──────────────────────────────────
  function openSocket(fileId, onMessage) {
    var attempt = 0;
    var ws = null;
    var closed = false;

    function connect() {
      ws = new WebSocket(wsUrl(fileId));

      ws.addEventListener('open', function () {
        attempt = 0;
      });

      ws.addEventListener('message', function (evt) {
        try {
          var data = JSON.parse(evt.data);
          if (data && data.type === 'presence') {
            onMessage(data.editors || []);
          }
        } catch (_) { /* ignore malformed */ }
      });

      ws.addEventListener('close', function () {
        if (closed) return;
        attempt += 1;
        var delay = Math.min(30000, 1000 * Math.pow(1.7, attempt));
        setTimeout(connect, delay);
      });

      ws.addEventListener('error', function () {
        try { ws.close(); } catch (_) {}
      });
    }

    connect();
    return { close: function () { closed = true; if (ws) ws.close(); } };
  }

  // ── Editor mode ────────────────────────────────────────────────────────────
  if (window.CE_FILE_ID) {
    openSocket(window.CE_FILE_ID, renderEditorMode);
  }

  // ── Grid mode ──────────────────────────────────────────────────────────────
  // Each card gets its own socket. This is fine for 20–30 cards on screen;
  // if you ever page with hundreds of cards per load, consider switching
  // to a single multiplexed connection. For now, simple wins.
  document.querySelectorAll('.ce-card-presence[data-file-id]').forEach(function (el) {
    var fileId = el.getAttribute('data-file-id');
    if (!fileId) return;
    openSocket(fileId, function (editors) { renderCard(el, editors); });
  });
})();
