/**
 * file_manager.js  —  EasyOffice File Manager
 * ─────────────────────────────────────────────
 * Single authoritative script. No duplicate declarations.
 * All mutating actions (upload, delete, share, rename, move, pin)
 * use fetch() and update the DOM in-place — zero full page reloads.
 *
 * Usage: include once at the bottom of file_manager.html
 *   <script src="{% static 'js/file_manager.js' %}"></script>
 */

(function () {
  'use strict';

  /* ═══════════════════════════════════════════════════════════════════════════
     SECTION 1 — CORE UTILITIES
  ═══════════════════════════════════════════════════════════════════════════ */

  function byId(id) { return document.getElementById(id); }

  function getCsrf() {
    var m = document.cookie.match(/(^|;)\s*csrftoken=([^;]+)/);
    return m ? m[2] : '';
  }

  /** Show a floating toast. Uses fmToast styling already defined in template. */
  function toast(msg, type) {
    var old = byId('fmToast');
    if (old) old.remove();

    var t = document.createElement('div');
    t.id = 'fmToast';
    var ok = type !== 'error';
    t.style.cssText = [
      'position:fixed', 'bottom:24px', 'right:24px', 'z-index:9999',
      'padding:13px 20px', 'border-radius:10px',
      'background:' + (ok ? '#10b981' : '#ef4444'),
      'color:#fff', 'font-size:.86rem', 'font-weight:600',
      'box-shadow:0 8px 32px rgba(0,0,0,.20)',
      'display:flex', 'align-items:center', 'gap:9px', 'max-width:360px',
      'opacity:0', 'transform:translateY(10px)',
      'transition:opacity .22s,transform .22s',
    ].join(';');
    t.innerHTML = '<i class="bi bi-' + (ok ? 'check-circle-fill' : 'exclamation-circle-fill') + '"></i>'
                + '<span>' + msg + '</span>';
    document.body.appendChild(t);
    requestAnimationFrame(function () {
      t.style.opacity = '1';
      t.style.transform = 'translateY(0)';
    });
    setTimeout(function () {
      t.style.opacity = '0';
      t.style.transform = 'translateY(10px)';
      setTimeout(function () { t.remove(); }, 260);
    }, 3800);
  }

  // Expose globally so inline onclick handlers can call it
  window.fmToast = toast;

  /**
   * POST formData (without CSRF — we append it here) to url.
   * Calls onOk(data) on {ok:true}, onError(msg) otherwise.
   */
  function post(url, formData, onOk, onError) {
    // Delete any existing CSRF token from the FormData (e.g. from FormData(form))
    // then re-append the current cookie value to avoid duplicates / stale tokens.
    try { formData.delete('csrfmiddlewaretoken'); } catch (_) {}
    formData.append('csrfmiddlewaretoken', getCsrf());
    fetch(url, {
      method: 'POST',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      credentials: 'same-origin',
      body: formData,
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) { onOk(d); }
        else { onError(d && d.error ? d.error : 'Something went wrong.'); }
      })
      .catch(function (err) {
        console.error('[fm]', url, err);
        onError('Network error — please try again.');
      });
  }

  /* ═══════════════════════════════════════════════════════════════════════════
     SECTION 2 — MODAL CONTROLS
  ═══════════════════════════════════════════════════════════════════════════ */

  function openModal(id) {
    var el = byId(id);
    if (el) {
      el.classList.add('open');
      document.body.style.overflow = 'hidden';
    }
  }

  function closeModal(id) {
    var el = byId(id);
    if (el) {
      el.classList.remove('open');
      document.body.style.overflow = '';
    }
  }

  window.openModal  = openModal;
  window.closeModal = closeModal;

  // Close on backdrop click
  document.querySelectorAll('.fm-modal-backdrop').forEach(function (bd) {
    bd.addEventListener('click', function (e) {
      if (e.target !== bd) return;
      if (bd.id === 'previewModal') { closePreviewModal(); }
      else { closeModal(bd.id); }
    });
  });

  // Close on Escape
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    document.querySelectorAll('.fm-modal-backdrop.open').forEach(function (m) {
      if (m.id === 'previewModal') { closePreviewModal(); }
      else { closeModal(m.id); }
    });
  });

  /* ═══════════════════════════════════════════════════════════════════════════
     SECTION 3 — DOM REFRESH HELPERS
     Update cards/rows in-place rather than reloading the page.
  ═══════════════════════════════════════════════════════════════════════════ */

  function removeFileCard(fileId) {
    // Find the top-level card wrapper and remove it
    var card = document.querySelector('[data-file-id="' + fileId + '"]');
    if (card) { card.remove(); return; }
    // list view row
    var row = document.querySelector('tr[data-file-id="' + fileId + '"]');
    if (row) { row.remove(); }
  }

  function removeFolderCard(folderId) {
    // grid card wrapper (has data-folder-id AND data-item-type=folder)
    var card = document.querySelector('[data-folder-id="' + folderId + '"]');
    if (card) { card.remove(); return; }
    // list view row
    var row = document.querySelector('tr[data-folder-id="' + folderId + '"]');
    if (row) { row.remove(); }
  }

  function updateFileName(fileId, newName) {
    // grid card
    var card = document.querySelector('[data-file-id="' + fileId + '"]');
    if (card) {
      var nameEl = card.querySelector('.file-name, .fm-card-name');
      if (nameEl) nameEl.textContent = newName;
    }
    // list row — first cell text
    var row = document.querySelector('tr[data-file-id="' + fileId + '"]');
    if (row) {
      var nameEl = row.querySelector('.fm-list-name-text, td a div');
      if (nameEl) nameEl.textContent = newName;
    }
  }

  function updateFolderName(folderId, newName) {
    // grid card
    var card = document.querySelector('[data-folder-id="' + folderId + '"]');
    if (card) {
      var nameEl = card.querySelector('.folder-name, .fm-card-name');
      if (nameEl) nameEl.textContent = newName;
    }
    // list row
    var row = document.querySelector('tr[data-folder-id="' + folderId + '"]');
    if (row) {
      var nameEl = row.querySelector('td a div');
      if (nameEl) nameEl.textContent = newName;
    }
  }

  /**
   * Soft-refresh only the file grid container via a partial fetch.
   * Falls back to full reload only if the server doesn't support ?partial=1.
   * Server-side: in FileManagerView.get(), add:
   *   if request.GET.get('partial') and _is_ajax(request):
   *       return render(request, 'files/_file_grid.html', ctx)
   */
  function refreshFileGrid() {
    var grid = (
      byId('fileGrid') || byId('file-grid') ||
      document.querySelector('.file-grid') ||
      document.querySelector('[data-file-grid]')
    );
    if (!grid) { window.location.reload(); return; }

    // Build the partial URL — preserve existing query params, add partial=1
    var qs   = window.location.search;
    var sep  = qs ? '&' : '?';
    var url  = window.location.pathname + qs + sep + 'partial=1';

    fetch(url, {
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      credentials: 'same-origin',
    })
      .then(function (r) {
        // If server signals partial is unavailable, do a quiet reload
        if (r.headers.get('X-Partial-Unavailable') === '1') {
          window.location.reload(); return Promise.reject('unavailable');
        }
        return r.ok ? r.text() : Promise.reject('bad-status');
      })
      .then(function (html) {
        if (!html) { window.location.reload(); return; }
        // If server returned full HTML page (no partial template), reload
        if (/^\s*<!DOCTYPE/i.test(html) || /^\s*<html/i.test(html)) {
          window.location.reload(); return;
        }
        grid.innerHTML = html;
        // Re-attach drag-and-drop to newly rendered cards
        attachDragHandlers();
        if (typeof window.initFileGrid === 'function') window.initFileGrid();
        // Re-run note badge checks on newly rendered buttons
        if (typeof window.runBulkCheck === 'function') window.runBulkCheck();
        // Restore current view (grid/list) preference
        var savedView = localStorage.getItem('eo_fm_view') || 'grid';
        setView(savedView);
        // Re-bind list-view sticky-column scroll buttons + overflow shadows.
        // The new grid's .fm-list-wrap has no listeners yet; this is a no-op
        // if the inline script in file_manager.html hasn't run yet.
        if (typeof window.fmInitListScroll === 'function') window.fmInitListScroll();
      })
      .catch(function (reason) {
        if (reason !== 'unavailable') window.location.reload();
      });
  }

  window.refreshFileGrid = refreshFileGrid;

  /* ── Intercept sort & search GET forms ─────────────────────────────────── */
  document.addEventListener('DOMContentLoaded', function () {

    /* Sort select — submit the form but swap the grid instead of full reload */
    var sortForm = document.getElementById('sortForm');
    if (sortForm) {
      sortForm.addEventListener('submit', function (e) {
        e.preventDefault();
        var params = new URLSearchParams(new FormData(sortForm));
        // Merge with existing params (folder, filter, type, q)
        var existing = new URLSearchParams(window.location.search);
        params.forEach(function (v, k) { existing.set(k, v); });
        var newUrl = window.location.pathname + '?' + existing.toString();
        history.pushState(null, '', newUrl);
        refreshFileGrid();
      });
    }

    /* Search form */
    var searchForm = document.querySelector('.fm-search');
    if (searchForm) {
      searchForm.addEventListener('submit', function (e) {
        e.preventDefault();
        var q = searchForm.querySelector('input[name="q"]');
        var existing = new URLSearchParams(window.location.search);
        if (q && q.value.trim()) { existing.set('q', q.value.trim()); }
        else { existing.delete('q'); }
        var newUrl = window.location.pathname + '?' + existing.toString();
        history.pushState(null, '', newUrl);
        refreshFileGrid();
      });
      /* Live search: 400ms debounce */
      var qInput = searchForm.querySelector('input[name="q"]');
      if (qInput) {
        var _st = null;
        qInput.addEventListener('input', function () {
          clearTimeout(_st);
          _st = setTimeout(function () {
            searchForm.dispatchEvent(new Event('submit', { bubbles: true }));
          }, 400);
        });
      }
    }

    /* Sidebar filter/folder links — intercept ?-prefixed hrefs */
    document.querySelectorAll('.fm-nav-item, .fm-folder-nav-item').forEach(function (a) {
      var href = a.getAttribute('href');
      if (!href || !href.startsWith('?')) return;
      a.addEventListener('click', function (e) {
        e.preventDefault();
        history.pushState(null, '', window.location.pathname + href);
        refreshFileGrid();
      });
    });

    /* Browser back/forward */
    window.addEventListener('popstate', function () {
      refreshFileGrid();
    });

  });

  /* ═══════════════════════════════════════════════════════════════════════════
     SECTION 4 — FORM SUBMIT INTERCEPTOR
     Hooks into submit events by URL pattern — no HTML changes needed.
  ═══════════════════════════════════════════════════════════════════════════ */

  var _interceptors = [];

  function intercept(pattern, handler) {
    _interceptors.push({ pattern: pattern, handler: handler });
  }

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || form.tagName !== 'FORM') return;
    // Grid/list delete forms carry .js-fm-delete and are owned by the
    // confirm-first flow in file_manager.html — never intercept them
    // here, or the file is deleted before the confirmation dialog shows.
    if (form.classList && form.classList.contains('js-fm-delete')) return;
    var action = form.action || '';
    for (var i = 0; i < _interceptors.length; i++) {
      if (_interceptors[i].pattern.test(action)) {
        e.preventDefault();
        e.stopPropagation();
        _interceptors[i].handler(form, e);
        return;
      }
    }
  }, true);

  // ── Upload ─────────────────────────────────────────────────────────────────
  intercept(/\/files\/upload\/?/, function (form) {
    var fd  = new FormData(form);
    var btn = form.querySelector('[type="submit"]');
    if (btn) { btn.disabled = true; btn.textContent = 'Uploading…'; }

    post(form.action, fd,
      function (d) {
        toast(d.message || 'File uploaded.', 'success');
        closeModal('uploadModal');
        refreshFileGrid();
        form.reset();
        if (btn) { btn.disabled = false; btn.textContent = 'Upload'; }
      },
      function (err) {
        toast(err, 'error');
        if (btn) { btn.disabled = false; btn.textContent = 'Upload'; }
      }
    );
  });

  // ── Delete file ─────────────────────────────────────────────────────────────
  intercept(/\/files\/[0-9a-f-]+\/delete\/?/, function (form) {
    var m = form.action.match(/\/files\/([0-9a-f-]+)\/delete/);
    var name = form.getAttribute('data-name') || form.getAttribute('data-file-name') || 'this file';
    var proceed = function () {
      post(form.action, new FormData(),
        function (d) {
          toast(d.message || 'File deleted.', 'success');
          if (m) removeFileCard(m[1]);
          closeModal('deleteFileModal');
          closeModal('deleteModal');
        },
        function (err) { toast(err, 'error'); }
      );
    };
    // ALWAYS confirm before deleting.
    if (typeof window.fmConfirmDelete === 'function') {
      window.fmConfirmDelete(name, 'file').then(function (ok) { if (ok) proceed(); });
    } else if (window.confirm('Delete \u201C' + name + '\u201D? This cannot be undone.')) {
      proceed();
    }
  });

  // ── Share file ──────────────────────────────────────────────────────────────
  intercept(/\/files\/[0-9a-f-]+\/share\/?/, function (form) {
    var fd  = new FormData(form);
    var btn = form.querySelector('[type="submit"]');
    if (btn) btn.disabled = true;
    post(form.action, fd,
      function (d) {
        toast(d.message || 'Sharing updated.', 'success');
        closeModal('shareModal');
        if (btn) btn.disabled = false;
      },
      function (err) {
        toast(err, 'error');
        if (btn) btn.disabled = false;
      }
    );
  });

  // ── Create folder ───────────────────────────────────────────────────────────
  intercept(/\/files\/folder\/create\/?/, function (form) {
    var fd = new FormData(form);
    post(form.action, fd,
      function (d) {
        toast(d.message || 'Folder created.', 'success');
        closeModal('createFolderModal');
        closeModal('newFolderModal');
        form.reset();
        refreshFileGrid();
      },
      function (err) { toast(err, 'error'); }
    );
  });

  // ── Delete folder ───────────────────────────────────────────────────────────
  intercept(/\/files\/folder\/[0-9a-f-]+\/delete\/?/, function (form) {
    var m = form.action.match(/\/files\/folder\/([0-9a-f-]+)\/delete/);
    var name = form.getAttribute('data-name') || 'this folder';
    var proceed = function () {
      post(form.action, new FormData(),
        function (d) {
          toast(d.message || 'Folder deleted.', 'success');
          if (m) removeFolderCard(m[1]);
          closeModal('deleteFolderModal');
          closeModal('deleteModal');
        },
        function (err) { toast(err, 'error'); }
      );
    };
    // ALWAYS confirm before deleting.
    if (typeof window.fmConfirmDelete === 'function') {
      window.fmConfirmDelete(name, 'folder').then(function (ok) { if (ok) proceed(); });
    } else if (window.confirm('Delete \u201C' + name + '\u201D? Files inside will be moved to the root folder.')) {
      proceed();
    }
  });

  // ── Share folder ────────────────────────────────────────────────────────────
  intercept(/\/files\/folder\/[0-9a-f-]+\/share\/?/, function (form) {
    var fd  = new FormData(form);
    var btn = form.querySelector('[type="submit"]');
    if (btn) btn.disabled = true;
    post(form.action, fd,
      function (d) {
        toast(d.message || 'Folder sharing updated.', 'success');
        closeModal('shareModal');
        if (btn) btn.disabled = false;
      },
      function (err) {
        toast(err, 'error');
        if (btn) btn.disabled = false;
      }
    );
  });

  // ── Rename file ─────────────────────────────────────────────────────────────
  intercept(/\/files\/[0-9a-f-]+\/rename\/?/, function (form) {
    var m  = form.action.match(/\/files\/([0-9a-f-]+)\/rename/);
    var fd = new FormData(form);
    post(form.action, fd,
      function (d) {
        toast(d.message || 'File renamed.', 'success');
        if (m && d.name) updateFileName(m[1], d.name);
        closeModal('renameModal');
      },
      function (err) { toast(err, 'error'); }
    );
  });

  // ── Rename folder ───────────────────────────────────────────────────────────
  intercept(/\/files\/folder\/[0-9a-f-]+\/rename\/?/, function (form) {
    var m  = form.action.match(/\/files\/folder\/([0-9a-f-]+)\/rename/);
    var fd = new FormData(form);
    post(form.action, fd,
      function (d) {
        toast(d.message || 'Folder renamed.', 'success');
        if (m && d.name) updateFolderName(m[1], d.name);
        closeModal('renameModal');
      },
      function (err) { toast(err, 'error'); }
    );
  });

  /* ═══════════════════════════════════════════════════════════════════════════
     SECTION 5 — MOVE MODAL
  ═══════════════════════════════════════════════════════════════════════════ */

  var _moveFileId      = null;
  var _moveFolderId    = '';

  window.openMoveModal = function (fileId, fileName) {
    _moveFileId   = fileId;
    _moveFolderId = '';
    if (byId('moveModalFileName')) byId('moveModalFileName').textContent = fileName;
    if (byId('moveConfirmBtn'))    byId('moveConfirmBtn').disabled = true;
    document.querySelectorAll('.move-folder-opt').forEach(function (btn) {
      btn.style.borderColor = 'var(--eo-border)';
      btn.style.background  = 'var(--eo-bg)';
    });
    openModal('moveModal');
  };

  window.selectMoveFolder = function (el) {
    _moveFolderId = el.getAttribute('data-folder-id') || '';
    document.querySelectorAll('.move-folder-opt').forEach(function (btn) {
      btn.style.borderColor = 'var(--eo-border)';
      btn.style.background  = 'var(--eo-bg)';
    });
    el.style.borderColor = 'var(--eo-accent)';
    el.style.background  = 'color-mix(in srgb, var(--eo-accent) 8%, white)';
    if (byId('moveConfirmBtn')) byId('moveConfirmBtn').disabled = false;
  };

  window.confirmMove = function () {
    if (!_moveFileId) return;
    var fd = new FormData();
    fd.append('folder_id', _moveFolderId || '');
    var url = '/files/' + _moveFileId + '/move/';

    fetch(url, {
      method: 'POST',
      headers: { 'X-CSRFToken': getCsrf(), 'X-Requested-With': 'XMLHttpRequest' },
      body: fd,
      credentials: 'same-origin',
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        closeModal('moveModal');
        if (d.status === 'ok') {
          toast(d.message || 'File moved.', 'success');
          // Remove card from current view — it now lives in another folder
          removeFileCard(_moveFileId);
        } else {
          toast(d.message || 'Move failed.', 'error');
        }
      })
      .catch(function () {
        closeModal('moveModal');
        toast('Move request failed.', 'error');
      });
  };

  /* ═══════════════════════════════════════════════════════════════════════════
     SECTION 6 — DRAG & DROP MOVE
  ═══════════════════════════════════════════════════════════════════════════ */

  var _dragType = null;
  var _dragId   = null;

  function setDropHighlight(el, on) {
    if (!el) return;
    el.classList.toggle('drop-active', on);
  }

  function attachDragHandlers() {
    document.querySelectorAll('.fm-draggable').forEach(function (el) {
      el.setAttribute('draggable', 'true');

      el.addEventListener('dragstart', function (e) {
        _dragType = el.dataset.itemType;
        _dragId   = el.dataset.itemId;
        if (!_dragType || !_dragId) return;
        el.classList.add('dragging-item');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', JSON.stringify({ itemType: _dragType, itemId: _dragId }));
      });

      el.addEventListener('dragend', function () {
        el.classList.remove('dragging-item');
        document.querySelectorAll('[data-drop-folder-id]').forEach(function (d) {
          setDropHighlight(d, false);
        });
      });
    });

    document.querySelectorAll('[data-drop-folder-id]').forEach(function (el) {
      el.addEventListener('dragenter', function (e) { e.preventDefault(); setDropHighlight(el, true); });
      el.addEventListener('dragover',  function (e) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; setDropHighlight(el, true); });
      el.addEventListener('dragleave', function (e) { if (!el.contains(e.relatedTarget)) setDropHighlight(el, false); });

      el.addEventListener('drop', function (e) {
        e.preventDefault();
        e.stopPropagation();
        setDropHighlight(el, false);

        var raw = e.dataTransfer.getData('text/plain');
        if (!raw) return;
        var payload;
        try { payload = JSON.parse(raw); } catch (_) { return; }

        var targetFolderId = el.dataset.dropFolderId || '';
        _doDragMove(payload.itemType, payload.itemId, targetFolderId);
      });
    });
  }

  function _doDragMove(itemType, itemId, targetFolderId) {
    var url = '';
    var fd  = new FormData();

    if (itemType === 'file') {
      url = '/files/' + itemId + '/move/';
      fd.append('folder_id', targetFolderId);
    } else if (itemType === 'folder') {
      url = '/files/folder/' + itemId + '/move/';
      fd.append('parent_id', targetFolderId);
    } else {
      return;
    }

    fetch(url, {
      method: 'POST',
      headers: { 'X-CSRFToken': getCsrf(), 'X-Requested-With': 'XMLHttpRequest' },
      body: fd,
      credentials: 'same-origin',
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status === 'ok') {
          toast(d.message || 'Moved.', 'success');
          // Remove the dragged card — it now belongs elsewhere
          if (itemType === 'file')   removeFileCard(itemId);
          if (itemType === 'folder') removeFolderCard(itemId);
        } else {
          toast(d.message || 'Move failed.', 'error');
        }
      })
      .catch(function () { toast('Move request failed.', 'error'); });
  }

  /* ═══════════════════════════════════════════════════════════════════════════
     SECTION 7 — PIN / UNPIN  (already JSON — just remove reload)
  ═══════════════════════════════════════════════════════════════════════════ */

  window.togglePin = function (type, id, btn) {
    var fd = new FormData();
    fd.append('type', type);
    fd.append('id', id);
    fd.append('csrfmiddlewaretoken', getCsrf());

    fetch('/files/pin/', { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var icon = btn.querySelector('i');
        if (d.pinned) {
          btn.classList.add('shared-active', 'shared-active-list');
          btn.title = 'Unpin';
          if (icon) icon.className = icon.className.replace('bi-pin-angle', 'bi-pin-angle-fill');
          toast('📌 Pinned to top', 'success');
        } else {
          btn.classList.remove('shared-active', 'shared-active-list');
          btn.title = 'Pin to top';
          if (icon) icon.className = icon.className.replace('bi-pin-angle-fill', 'bi-pin-angle');
          toast('📌 Unpinned', 'success');
        }
        // Move the card to top / back without a full reload
        var card = btn.closest('[data-file-id],[data-folder-id],tr');
        if (card) {
          var grid = card.parentElement;
          if (grid) {
            if (d.pinned) { grid.prepend(card); }
            else {
              // Re-sort: move after last pinned item
              var pinned = grid.querySelectorAll('.pinned-card, [data-pinned="true"]');
              var lastPinned = pinned.length ? pinned[pinned.length - 1] : null;
              if (lastPinned && lastPinned.nextSibling) {
                grid.insertBefore(card, lastPinned.nextSibling);
              }
            }
          }
        }
      })
      .catch(function () { toast('Failed to update pin.', 'error'); });
  };

  /* ═══════════════════════════════════════════════════════════════════════════
     SECTION 8 — RENAME MODAL OPENER
  ═══════════════════════════════════════════════════════════════════════════ */

  window.openRenameModal = function (type, id, currentName) {
    var form    = byId('renameForm');
    var input   = byId('renameInput');
    var subtext = byId('renameModalSubtext');
    if (!form || !input) return;

    if (type === 'file') {
      form.action = '/files/' + id + '/rename/';
      if (subtext) subtext.textContent = 'Rename file';
    } else {
      form.action = '/files/folder/' + id + '/rename/';
      if (subtext) subtext.textContent = 'Rename folder';
    }

    input.value = currentName || '';
    openModal('renameModal');
    setTimeout(function () { input.focus(); input.select(); }, 80);
  };

  /* ═══════════════════════════════════════════════════════════════════════════
     SECTION 9 — VIEW TOGGLE  (grid / list)
  ═══════════════════════════════════════════════════════════════════════════ */

  var VIEW_KEY = 'eo_fm_view';

  window.setView = function (v) {
    if (byId('viewGrid')) byId('viewGrid').style.display = v === 'grid' ? '' : 'none';
    if (byId('viewList')) byId('viewList').style.display = v === 'list' ? '' : 'none';
    if (byId('btnGrid'))  byId('btnGrid').classList.toggle('active', v === 'grid');
    if (byId('btnList'))  byId('btnList').classList.toggle('active', v === 'list');
    localStorage.setItem(VIEW_KEY, v);
  };

  (function () {
    setView(localStorage.getItem(VIEW_KEY) || 'grid');
  })();

  /* ═══════════════════════════════════════════════════════════════════════════
     SECTION 10 — MOBILE SIDEBAR
  ═══════════════════════════════════════════════════════════════════════════ */

  window.toggleMobileSidebar = function () {
    var sidebar = document.querySelector('.fm-sidebar');
    var btn     = byId('sidebarToggle');
    if (!sidebar) return;
    var open = sidebar.classList.toggle('mob-open');
    if (btn) btn.classList.toggle('active', open);
  };

  (function () {
    var sidebar = document.querySelector('.fm-sidebar');
    var btn     = byId('sidebarToggle');
    if (!sidebar || !btn) return;
    if (window.innerWidth > 768) {
      sidebar.classList.add('mob-open');
      btn.classList.add('active');
    }
    window.addEventListener('resize', function () {
      if (window.innerWidth > 768) {
        sidebar.classList.add('mob-open');
        btn.classList.add('active');
      }
    });
  })();

  /* ═══════════════════════════════════════════════════════════════════════════
     SECTION 11 — UPLOAD  (file input + drag-onto-dropzone)
  ═══════════════════════════════════════════════════════════════════════════ */

  function formatBytes(b) {
    if (b < 1024)    return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    return (b / 1048576).toFixed(1) + ' MB';
  }

  window.handleFileChange = function (input) {
    var f          = input.files && input.files[0];
    var sel        = byId('selectedFile');
    var uploadBtn  = byId('uploadBtn');
    if (!sel || !uploadBtn) return;
    if (f) {
      if (byId('selectedFileName')) byId('selectedFileName').textContent = f.name;
      if (byId('selectedFileSize')) byId('selectedFileSize').textContent = formatBytes(f.size);
      sel.style.display = 'flex';
      uploadBtn.disabled = false;
    } else {
      sel.style.display = 'none';
      uploadBtn.disabled = true;
    }
  };

  document.addEventListener('DOMContentLoaded', function () {
    var dropZone  = byId('dropZone');
    var fileInput = byId('fileInput');
    if (!dropZone || !fileInput) return;

    function stop(e) { e.preventDefault(); e.stopPropagation(); }

    ['dragenter','dragover','dragleave','drop'].forEach(function (ev) {
      dropZone.addEventListener(ev, stop);
      document.body.addEventListener(ev, stop);
    });
    ['dragenter','dragover'].forEach(function (ev) {
      dropZone.addEventListener(ev, function () { dropZone.classList.add('dragging'); });
    });
    ['dragleave','drop'].forEach(function (ev) {
      dropZone.addEventListener(ev, function () { dropZone.classList.remove('dragging'); });
    });

    dropZone.addEventListener('drop', function (e) {
      var files = e.dataTransfer && e.dataTransfer.files;
      if (!files || !files.length) return;
      try {
        var dt = new DataTransfer();
        for (var i = 0; i < files.length; i++) dt.items.add(files[i]);
        fileInput.files = dt.files;
      } catch (_) { fileInput.files = files; }
      handleFileChange(fileInput);
    });

    fileInput.addEventListener('change', function () { handleFileChange(fileInput); });

    // Attach drag-move handlers after DOM ready
    attachDragHandlers();
  });


  /* ═══════════════════════════════════════════════════════════════════════════
     SECTION 11b — FOLDER UPLOAD  (directory picker + folder drag-and-drop)

     A browser cannot post a directory. It posts a flat list of files, each
     carrying the path it had on disk. So the job here is:

       1. collect {file, path} pairs — from a webkitdirectory input, or by
          walking the entry tree of a drag-and-drop;
       2. send them to /files/upload-folder/ in batches, SEQUENTIALLY;
       3. show progress, because a real folder takes a while.

     Batches are sequential on purpose. The server creates each folder with a
     get-or-create keyed on (name, parent, owner); two batches in flight at
     once would both look up "Reports", both miss, and both create it. One
     request at a time keeps the tree correct at the cost of some throughput.
  ═══════════════════════════════════════════════════════════════════════════ */

  var FOLDER_UPLOAD_URL   = '/files/upload-folder/';
  var BATCH_MAX_FILES     = 25;
  var BATCH_MAX_BYTES     = 20 * 1024 * 1024;   // keep each POST modest
  var TRAVERSAL_MAX_FILES = 2000;               // guard against a dropped $HOME

  /** Destination folder: the upload form's hidden field, else the URL. */
  function currentFolderId() {
    var input = document.querySelector('#uploadForm [name="folder_id"]')
             || document.querySelector('[name="folder_id"]');
    if (input && input.value) return input.value;

    var m = window.location.search.match(/[?&]folder=([0-9a-f-]{36})/i);
    return m ? m[1] : '';
  }

  function currentProjectId() {
    var input = document.querySelector('[name="project_id"]');
    return input && input.value ? input.value : '';
  }

  /* ── Progress panel ──────────────────────────────────────────────────────
     Built in JS so this works whether or not file_manager.html has been
     updated with the markup. */

  var _fuPanel = null;

  function folderProgressOpen(label) {
    if (!_fuPanel) {
      _fuPanel = document.createElement('div');
      _fuPanel.id = 'folderUploadProgress';
      _fuPanel.style.cssText =
        'position:fixed;right:20px;bottom:20px;z-index:9999;width:320px;' +
        'background:var(--eo-surface,#fff);border:1px solid var(--eo-border,#e2e8f0);' +
        'border-radius:12px;box-shadow:0 10px 30px rgba(15,23,42,.18);padding:14px 16px;' +
        'font-size:.82rem;color:var(--eo-text,#0f172a)';
      _fuPanel.innerHTML =
        '<div style="display:flex;align-items:center;gap:9px;margin-bottom:8px">' +
          '<i class="bi bi-folder-plus" style="color:var(--eo-accent,#2196f3)"></i>' +
          '<strong id="fuTitle" style="flex:1;font-size:.85rem"></strong>' +
          '<button type="button" id="fuClose" style="background:none;border:none;' +
            'cursor:pointer;color:var(--eo-text-muted,#64748b);display:none">' +
            '<i class="bi bi-x-lg"></i></button>' +
        '</div>' +
        '<div id="fuDetail" style="color:var(--eo-text-muted,#64748b);margin-bottom:8px"></div>' +
        '<div style="height:6px;background:var(--eo-border,#e2e8f0);border-radius:3px;overflow:hidden">' +
          '<div id="fuBar" style="height:100%;width:0%;background:var(--eo-accent,#2196f3);' +
            'border-radius:3px;transition:width .2s"></div>' +
        '</div>';
      document.body.appendChild(_fuPanel);
      _fuPanel.querySelector('#fuClose').addEventListener('click', folderProgressClose);
    }
    _fuPanel.style.display = '';
    _fuPanel.querySelector('#fuTitle').textContent = label;
    _fuPanel.querySelector('#fuClose').style.display = 'none';
    return _fuPanel;
  }

  function folderProgressUpdate(done, total, detail) {
    if (!_fuPanel) return;
    var pct = total ? Math.round((done / total) * 100) : 0;
    _fuPanel.querySelector('#fuBar').style.width = pct + '%';
    _fuPanel.querySelector('#fuDetail').textContent =
      detail || (done + ' of ' + total + ' files · ' + pct + '%');
  }

  function folderProgressDone(text) {
    if (!_fuPanel) return;
    _fuPanel.querySelector('#fuBar').style.width = '100%';
    _fuPanel.querySelector('#fuTitle').textContent = 'Upload complete';
    _fuPanel.querySelector('#fuDetail').textContent = text;
    _fuPanel.querySelector('#fuClose').style.display = '';
    setTimeout(folderProgressClose, 6000);
  }

  function folderProgressClose() {
    if (_fuPanel) _fuPanel.style.display = 'none';
  }

  /* ── Batching and upload ─────────────────────────────────────────────── */

  function buildBatches(entries) {
    var batches = [], current = [], bytes = 0;
    for (var i = 0; i < entries.length; i++) {
      var size = entries[i].file.size || 0;
      var wouldOverflow = current.length >= BATCH_MAX_FILES ||
                          (current.length && bytes + size > BATCH_MAX_BYTES);
      if (wouldOverflow) { batches.push(current); current = []; bytes = 0; }
      current.push(entries[i]);
      bytes += size;
    }
    if (current.length) batches.push(current);
    return batches;
  }

  function postBatch(batch) {
    return new Promise(function (resolve) {
      var fd = new FormData();
      var folderId  = currentFolderId();
      var projectId = currentProjectId();

      if (folderId)  fd.append('folder_id', folderId);
      if (projectId) fd.append('project_id', projectId);

      var visibility = document.querySelector('#uploadForm [name="visibility"]');
      fd.append('visibility', visibility && visibility.value ? visibility.value : 'private');

      for (var i = 0; i < batch.length; i++) {
        fd.append('files[]', batch[i].file);
        fd.append('paths[]', batch[i].path);
      }

      post(FOLDER_UPLOAD_URL, fd,
        function (d) { resolve({ ok: true, data: d }); },
        function (err) { resolve({ ok: false, error: err }); }
      );
    });
  }

  function uploadFolderEntries(entries, label) {
    if (!entries.length) { toast('That folder is empty.', 'error'); return; }

    if (entries.length > TRAVERSAL_MAX_FILES) {
      toast('That folder has ' + entries.length + ' files — over the ' +
            TRAVERSAL_MAX_FILES + ' limit for one upload.', 'error');
      return;
    }

    var batches = buildBatches(entries);
    var totalFiles = entries.length;
    var doneFiles = 0, stored = 0, folders = 0, skipped = 0, failed = 0;

    folderProgressOpen('Uploading ' + label);
    folderProgressUpdate(0, totalFiles);

    // Sequential — see the note at the top of this section.
    var chain = Promise.resolve();
    batches.forEach(function (batch) {
      chain = chain.then(function () {
        return postBatch(batch).then(function (res) {
          doneFiles += batch.length;
          if (res.ok) {
            stored  += res.data.stored || 0;
            folders += res.data.folders_created || 0;
            skipped += res.data.skipped_count || 0;
            failed  += res.data.failed_count || 0;
          } else {
            failed += batch.length;
          }
          folderProgressUpdate(doneFiles, totalFiles);
        });
      });
    });

    chain.then(function () {
      var parts = [stored + ' file' + (stored === 1 ? '' : 's')];
      if (folders) parts.push(folders + ' folder' + (folders === 1 ? '' : 's'));
      var summary = parts.join(', ') + ' uploaded';
      if (skipped) summary += ' · ' + skipped + ' skipped';
      if (failed)  summary += ' · ' + failed + ' failed';

      folderProgressDone(summary);
      toast(summary + '.', failed ? 'error' : 'success');
      closeModal('uploadModal');
      refreshFileGrid();
    });
  }

  /* ── Source 1: the directory picker ──────────────────────────────────── */

  window.handleFolderChange = function (input) {
    var files = input && input.files;
    if (!files || !files.length) return;

    var entries = [];
    for (var i = 0; i < files.length; i++) {
      entries.push({
        file: files[i],
        path: files[i].webkitRelativePath || files[i].name,
      });
    }

    var first = entries[0].path.split('/')[0];
    uploadFolderEntries(entries, '"' + first + '"');
    input.value = '';
  };

  /* ── Source 2: dragging a folder onto the dropzone ───────────────────── */

  /**
   * readEntries() returns at most 100 children per call — a folder with 250
   * files silently loses 150 of them if you call it once. Keep calling until
   * it returns an empty array.
   */
  function readAllEntries(reader) {
    return new Promise(function (resolve, reject) {
      var all = [];
      (function next() {
        reader.readEntries(function (batch) {
          if (!batch.length) { resolve(all); return; }
          all = all.concat(Array.prototype.slice.call(batch));
          next();
        }, reject);
      })();
    });
  }

  function walkEntry(entry, prefix, out) {
    if (!entry) return Promise.resolve();

    if (entry.isFile) {
      return new Promise(function (resolve) {
        entry.file(function (file) {
          out.push({ file: file, path: prefix + entry.name });
          resolve();
        }, function () { resolve(); });   // unreadable file — skip, don't abort
      });
    }

    if (entry.isDirectory) {
      var reader = entry.createReader();
      return readAllEntries(reader).then(function (children) {
        return children.reduce(function (chain, child) {
          return chain.then(function () {
            return walkEntry(child, prefix + entry.name + '/', out);
          });
        }, Promise.resolve());
      }).catch(function () { /* unreadable directory — skip */ });
    }

    return Promise.resolve();
  }

  function handleDroppedItems(items) {
    var roots = [];
    for (var i = 0; i < items.length; i++) {
      var entry = items[i].webkitGetAsEntry && items[i].webkitGetAsEntry();
      if (entry) roots.push(entry);
    }
    if (!roots.length) return null;

    var hasDirectory = roots.some(function (e) { return e.isDirectory; });
    if (!hasDirectory) return null;      // plain files — let the old path run

    var out = [];
    folderProgressOpen('Reading folder…');
    folderProgressUpdate(0, 1, 'Scanning…');

    return roots.reduce(function (chain, entry) {
      return chain.then(function () { return walkEntry(entry, '', out); });
    }, Promise.resolve()).then(function () {
      var label = roots.length === 1 ? '"' + roots[0].name + '"'
                                     : roots.length + ' folders';
      uploadFolderEntries(out, label);
    });
  }

  /* ── Wiring ──────────────────────────────────────────────────────────── */

  document.addEventListener('DOMContentLoaded', function () {
    var dropZone = byId('dropZone');

    // Use the template's input when it exists; create one when it doesn't, so
    // this works without a file_manager.html change.
    var folderInput = byId('folderInput');
    if (!folderInput) {
      folderInput = document.createElement('input');
      folderInput.type = 'file';
      folderInput.id = 'folderInput';
      folderInput.multiple = true;
      folderInput.style.display = 'none';
      folderInput.setAttribute('webkitdirectory', '');
      folderInput.setAttribute('directory', '');
      document.body.appendChild(folderInput);
    }
    folderInput.addEventListener('change', function () {
      window.handleFolderChange(folderInput);
    });

    window.pickFolder = function () { folderInput.click(); };

    // Offer the action inside the upload modal if the template has no button.
    if (dropZone && !byId('pickFolderBtn')) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.id = 'pickFolderBtn';
      btn.className = 'eo-btn eo-btn-secondary eo-btn-sm';
      btn.style.cssText = 'margin-top:10px;width:100%';
      btn.innerHTML = '<i class="bi bi-folder-plus"></i> Upload a folder instead';
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        window.pickFolder();
      });
      dropZone.parentNode.insertBefore(btn, dropZone.nextSibling);
    }

    // Folder-aware drop. Runs at capture time so it can claim the event
    // before the single-file handler below sees it.
    if (dropZone) {
      dropZone.addEventListener('drop', function (e) {
        var items = e.dataTransfer && e.dataTransfer.items;
        if (!items || !items.length) return;
        var handled = handleDroppedItems(items);
        if (handled) {
          e.preventDefault();
          e.stopPropagation();
        }
      }, true);
    }
  });

  /* ═══════════════════════════════════════════════════════════════════════════
     SECTION 12 — NEW FOLDER SCOPE PICKER
  ═══════════════════════════════════════════════════════════════════════════ */

  window.toggleNewFolderScope = function () {
    var vis = byId('newFolderVisibility');
    if (!vis) return;
    if (byId('newFolderUnitWrap')) byId('newFolderUnitWrap').style.display = vis.value === 'unit'       ? '' : 'none';
    if (byId('newFolderDeptWrap')) byId('newFolderDeptWrap').style.display = vis.value === 'department' ? '' : 'none';
  };
  window.toggleNewFolderScope();

  /* ═══════════════════════════════════════════════════════════════════════════
     SECTION 13 — SHARE MODAL
  ═══════════════════════════════════════════════════════════════════════════ */

  window.togglePermission = function (checkbox, userId) {
    var sel = byId('perm_' + userId);
    if (!sel) return;
    sel.disabled = !checkbox.checked;
    if (checkbox.checked && !sel.value) sel.value = 'view';
  };

  window.showSubPickers = function (vis) {
    var isFolder = byId('shareItemType') && byId('shareItemType').value === 'folder';
    if (byId('peoplePicker')) byId('peoplePicker').style.display = (!isFolder && vis === 'shared_with') ? '' : 'none';
    if (byId('unitPicker'))   byId('unitPicker').style.display   = vis === 'unit'       ? '' : 'none';
    if (byId('deptPicker'))   byId('deptPicker').style.display   = vis === 'department' ? '' : 'none';
  };

  window.filterPeople = function (query) {
    query = (query || '').toLowerCase();
    document.querySelectorAll('#peopleList [data-name]').forEach(function (row) {
      row.style.display = row.dataset.name.indexOf(query) !== -1 ? '' : 'none';
    });
  };

  window.countSelected = function () {
    var n = document.querySelectorAll('#peopleList input[type="checkbox"]:checked').length;
    if (byId('selectedCount')) byId('selectedCount').textContent = n;
  };

  window.updateStopSharingButton = function (vis) {
    var btn = byId('stopSharingBtn');
    if (btn) btn.style.display = (vis && vis !== 'private') ? '' : 'none';
  };

  window.onVisChange = function (radio) {
    document.querySelectorAll('.vis-opt').forEach(function (el) {
      el.style.borderColor = 'var(--eo-border)';
    });
    if (radio && radio.closest('.vis-opt')) {
      radio.closest('.vis-opt').style.borderColor = 'var(--eo-accent)';
      showSubPickers(radio.value);
      updateStopSharingButton(radio.value);
    }
  };

  window.stopSharingNow = function () {
    var privateRadio = document.querySelector('#shareForm input[name="visibility"][value="private"]');
    if (privateRadio) { privateRadio.checked = true; onVisChange(privateRadio); }
    document.querySelectorAll('#peopleList input[type="checkbox"]').forEach(function (cb) { cb.checked = false; });
    document.querySelectorAll('#peopleList select[id^="perm_"]').forEach(function (sel) { sel.disabled = true; sel.value = 'view'; });
    var u = document.querySelector('#unitPicker select[name="unit_id"]');
    if (u) u.value = '';
    var d = document.querySelector('#deptPicker select[name="dept_id"]');
    if (d) d.value = '';
    countSelected();
  };

  window.openShareModal = function (id, name, currentVisibility, type, sharedIds, unitId, deptId, permissions, viewerPermission, opts) {
    opts            = opts || {};
    type            = type || 'file';
    sharedIds       = Array.isArray(sharedIds) ? sharedIds : [];
    unitId          = unitId || '';
    deptId          = deptId || '';
    permissions     = permissions || {};
    viewerPermission = viewerPermission || '';

    var isFolder    = type === 'folder';
    var formAction  = isFolder ? ('/files/folder/' + id + '/share/') : ('/files/' + id + '/share/');

    if (byId('shareForm'))         byId('shareForm').action = formAction;
    if (byId('shareItemType'))     byId('shareItemType').value = type;
    if (byId('shareModalFileName')) byId('shareModalFileName').textContent = name;
    if (byId('shareModalTitle'))   byId('shareModalTitle').textContent    = isFolder ? 'Share Folder' : 'Share File';
    if (byId('shareModalQuestion')) byId('shareModalQuestion').textContent = isFolder ? 'Who can access this folder?' : 'Who can access this file?';

    // Stamp the current item's id onto the form dataset so the
    // External People tab can resolve which file to share. Folders
    // don't support external shares, so the External tab will hide
    // itself (it reads shareItemType to decide).
    var _shareFormEl = byId('shareForm');
    if (_shareFormEl) {
      _shareFormEl.dataset.currentFileId = isFolder ? '' : id;
      _shareFormEl.dataset.currentItemId = id;
      _shareFormEl.dataset.currentItemType = type;
    }

    if (byId('folderShareOptions')) byId('folderShareOptions').style.display = isFolder ? '' : 'none';
    if (byId('fileShareOptions'))   byId('fileShareOptions').style.display   = isFolder ? 'none' : '';
    if (byId('shareChildrenChk'))        byId('shareChildrenChk').checked        = !!opts.shareChildren;
    if (byId('inheritParentSharingChk')) byId('inheritParentSharingChk').checked = opts.inheritParentSharing !== false;
    if (byId('inheritFolderSharingChk')) byId('inheritFolderSharingChk').checked = opts.inheritFolderSharing !== false;

    // Permission banner
    var banner = byId('yourPermBanner');
    if (banner && viewerPermission) {
      var meta = {
        full: { label:'Full Control', desc:'You can share, edit, manage, and delete.',  color:'#10b981', bg:'#d1fae5', icon:'bi-shield-fill-check' },
        edit: { label:'Edit Access',  desc:'You can share, move, convert — not delete.', color:'#3b82f6', bg:'#dbeafe', icon:'bi-pencil-fill' },
        view: { label:'View Only',    desc:'You can preview and download.',              color:'#64748b', bg:'#f1f5f9', icon:'bi-eye-fill' },
      }[viewerPermission] || { label:'View Only', desc:'', color:'#64748b', bg:'#f1f5f9', icon:'bi-eye-fill' };

      banner.style.display     = 'flex';
      banner.style.borderColor = meta.color + '40';
      banner.style.background  = meta.bg + '60';
      var iw = byId('yourPermIcon');
      if (iw) { iw.style.background = meta.color + '18'; iw.innerHTML = '<i class="bi ' + meta.icon + '" style="font-size:1rem;color:' + meta.color + '"></i>'; }
      if (byId('yourPermLabel')) { byId('yourPermLabel').textContent = meta.label; byId('yourPermLabel').style.color = meta.color; }
      if (byId('yourPermDesc'))  byId('yourPermDesc').textContent = meta.desc;
      var badge = byId('yourPermBadge');
      if (badge) { badge.textContent = viewerPermission.charAt(0).toUpperCase() + viewerPermission.slice(1); badge.style.background = meta.color + '18'; badge.style.color = meta.color; }
    } else if (banner) {
      banner.style.display = 'none';
    }

    var swo = byId('opt-shared_with');
    if (swo) swo.style.display = isFolder ? 'none' : '';

    // Reset state
    document.querySelectorAll('#shareForm .vis-opt').forEach(function (el) { el.style.borderColor = 'var(--eo-border)'; });
    document.querySelectorAll('#shareForm input[name="visibility"]').forEach(function (r) { r.checked = false; });
    document.querySelectorAll('#peopleList input[type="checkbox"]').forEach(function (cb) { cb.checked = false; });
    document.querySelectorAll('#peopleList select[id^="perm_"]').forEach(function (s) { s.disabled = true; s.value = 'view'; });

    // Restore direct shares
    sharedIds.forEach(function (uid) {
      var cb = document.querySelector('#peopleList input[type="checkbox"][value="' + uid + '"]');
      if (!cb) return;
      cb.checked = true;
      togglePermission(cb, uid);
      var ps = byId('perm_' + uid);
      if (ps && permissions[uid]) ps.value = permissions[uid];
    });

    var us = document.querySelector('#unitPicker select[name="unit_id"]');
    if (us) us.value = unitId;
    var ds = document.querySelector('#deptPicker select[name="dept_id"]');
    if (ds) ds.value = deptId;

    var effectiveVis = (isFolder && currentVisibility === 'shared_with') ? 'private' : currentVisibility;
    var tr = document.querySelector('#shareForm input[name="visibility"][value="' + effectiveVis + '"]')
          || document.querySelector('#shareForm input[name="visibility"][value="private"]');
    if (tr) { tr.checked = true; onVisChange(tr); }

    countSelected();
    updateStopSharingButton(effectiveVis);
    openModal('shareModal');
  };

  /* ═══════════════════════════════════════════════════════════════════════════
     SECTION 14 — PREVIEW MODAL
  ═══════════════════════════════════════════════════════════════════════════ */

  window.openPreviewModal = function (fileId, fileName, extension) {
    var content     = byId('previewContent');
    var loading     = byId('previewLoading');
    var nameEl      = byId('previewModalFileName');
    var downloadBtn = byId('previewDownloadBtn');
    if (!content || !loading) return;

    if (nameEl)      nameEl.textContent = fileName;
    if (downloadBtn) downloadBtn.href   = '/files/' + fileId + '/download/';

    content.innerHTML    = '';
    content.style.display = 'none';
    loading.style.display = 'flex';
    loading.innerHTML = '<div style="text-align:center"><i class="bi bi-hourglass-split" style="font-size:1.8rem;display:block;margin-bottom:10px"></i>Loading preview...</div>';
    openModal('previewModal');

    var ext        = (extension || '').toLowerCase();
    var previewUrl = '/files/' + fileId + '/preview/';

    function show(html) {
      content.innerHTML     = html;
      loading.style.display = 'none';
      content.style.display = 'block';
    }

    if (ext === 'pdf') {
      show('<object class="preview-frame" data="' + previewUrl + '" type="application/pdf">'
         + '<div style="height:100%;display:flex;align-items:center;justify-content:center;text-align:center;padding:24px">'
         + '<div><i class="bi bi-file-earmark-pdf" style="font-size:2rem;display:block;margin-bottom:10px;color:#ef4444"></i>'
         + '<div style="font-weight:700;margin-bottom:6px">PDF preview unavailable in this browser.</div>'
         + '<a href="/files/' + fileId + '/download/" class="eo-btn eo-btn-secondary eo-btn-sm"><i class="bi bi-download"></i> Download PDF</a>'
         + '</div></div></object>');
      return;
    }

    if (ext === 'zip') {
      show('<iframe class="preview-frame" src="' + previewUrl + '"></iframe>');
      return;
    }

    var officeExts = ['doc','docx','odt','rtf','xls','xlsx','ods','ppt','pptx','odp'];
    if (officeExts.indexOf(ext) !== -1) {
      loading.innerHTML = '<div style="text-align:center">'
        + '<div style="width:36px;height:36px;border:3px solid var(--eo-border);border-top-color:var(--eo-accent);border-radius:50%;animation:spin .7s linear infinite;margin:0 auto 14px"></div>'
        + '<div style="font-size:.9rem;font-weight:600;margin-bottom:4px">Converting to PDF…</div>'
        + '<div style="font-size:.78rem;color:var(--eo-text-muted)">LibreOffice is rendering your document</div></div>';

      fetch(previewUrl, { credentials: 'same-origin' })
        .then(function (r) { if (!r.ok) throw new Error(); return r.blob(); })
        .then(function (blob) { show('<iframe class="preview-frame" src="' + URL.createObjectURL(blob) + '"></iframe>'); })
        .catch(function () {
          show('<div style="height:100%;display:flex;align-items:center;justify-content:center;text-align:center;padding:24px">'
             + '<div><i class="bi bi-file-earmark-x" style="font-size:2.4rem;display:block;margin-bottom:12px;color:#f87171"></i>'
             + '<div style="font-weight:700;margin-bottom:6px">Office preview failed</div>'
             + '<div style="font-size:.82rem;margin-bottom:16px">Download it to open locally.</div>'
             + '<a href="/files/' + fileId + '/download/" class="eo-btn eo-btn-primary eo-btn-sm"><i class="bi bi-download"></i> Download file</a>'
             + '</div></div>');
        });
      return;
    }

    if (['jpg','jpeg','png','gif','webp','svg','bmp'].indexOf(ext) !== -1) {
      show('<img class="preview-image" src="' + previewUrl + '" alt="">');
      return;
    }

    if (['mp4','webm','ogg','mov'].indexOf(ext) !== -1) {
      show('<video class="preview-video" controls><source src="' + previewUrl + '">Video not supported.</video>');
      return;
    }

    if (['mp3','wav','m4a'].indexOf(ext) !== -1) {
      show('<div class="preview-audio"><audio controls style="width:min(720px,92%)"><source src="' + previewUrl + '">Audio not supported.</audio></div>');
      return;
    }

    if (['txt','md','py','js','html','css','json','xml','sql','csv','yml','yaml','ini','log'].indexOf(ext) !== -1) {
      fetch(previewUrl, { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
        .then(function (r) { return r.text(); })
        .then(function (text) {
          content.innerHTML = '<pre class="preview-text"></pre>';
          content.querySelector('.preview-text').textContent = text;
          loading.style.display = 'none';
          content.style.display = 'block';
        })
        .catch(function () { show('<div class="preview-text">Unable to preview this text file.</div>'); });
      return;
    }

    show('<div style="height:100%;display:flex;align-items:center;justify-content:center;text-align:center;padding:24px">'
       + '<div><i class="bi bi-file-earmark" style="font-size:2rem;display:block;margin-bottom:10px"></i>'
       + '<div style="font-weight:700;margin-bottom:6px">Preview not available for this file type</div>'
       + '<div style="font-size:.85rem">Use the download button to open locally.</div>'
       + '</div></div>');
  };

  window.closePreviewModal = function () {
    var content = byId('previewContent');
    var loading = byId('previewLoading');
    if (content) { content.innerHTML = ''; content.style.display = 'none'; }
    if (loading) {
      loading.style.display = 'flex';
      loading.innerHTML = '<div style="text-align:center"><i class="bi bi-hourglass-split" style="font-size:1.8rem;display:block;margin-bottom:10px"></i>Loading preview...</div>';
    }
    closeModal('previewModal');
  };

  /* ── Stop-sharing: also revoke all note shares for that file ─────────── */
  /* Wraps the stopSharingNow() defined earlier in this file. */
  (function () {
    var _origStop = window.stopSharingNow;
    window.stopSharingNow = function () {
      /* Call the original UI reset first */
      if (typeof _origStop === 'function') _origStop();

      /* Then silently wipe note shares */
      var shareForm = byId('shareForm');
      var fileId = shareForm ? shareForm.dataset.currentFileId : null;
      if (!fileId) return;

      var ol  = document.getElementById('fnOverlay');
      var tpl = ol ? (ol.dataset.noteShareUrl || '') : '';
      if (!tpl) return;
      var url = tpl.replace('00000000-0000-0000-0000-000000000000', fileId) + 'revoke_all/';
      fetch(url, {
        method: 'POST',
        headers: { 'X-Requested-With': 'XMLHttpRequest', 'X-CSRFToken': getCsrf() },
        credentials: 'same-origin',
      }).catch(function () {});
    };
  })();

})(); // ← end of IIFE