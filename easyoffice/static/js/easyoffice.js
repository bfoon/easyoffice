/* EasyOffice — Main JS */
(function () {
  'use strict';

  const sidebar = document.getElementById('sidebar');
  const topbar = document.getElementById('topbar');
  const main = document.getElementById('main');
  const toggle = document.getElementById('sidebarToggle');
  const COLLAPSED_KEY = 'eo_sidebar_collapsed';

  // Sidebar toggle
  if (toggle && sidebar) {
    const collapsed = localStorage.getItem(COLLAPSED_KEY) === '1';
    if (collapsed) _collapse();

    toggle.addEventListener('click', function () {
      if (window.innerWidth < 992) {
        sidebar.classList.toggle('mobile-open');
      } else {
        sidebar.classList.contains('collapsed') ? _expand() : _collapse();
      }
    });

    function _collapse() {
      sidebar.classList.add('collapsed');
      topbar && topbar.classList.add('expanded');
      main && main.classList.add('expanded');
      localStorage.setItem(COLLAPSED_KEY, '1');
    }
    function _expand() {
      sidebar.classList.remove('collapsed');
      topbar && topbar.classList.remove('expanded');
      main && main.classList.remove('expanded');
      localStorage.setItem(COLLAPSED_KEY, '0');
    }
  }

  // Auto-dismiss alerts
  document.querySelectorAll('.eo-alert').forEach(function (el) {
    setTimeout(function () {
      var bsAlert = bootstrap.Alert.getOrCreateInstance(el);
      if (bsAlert) bsAlert.close();
    }, 5000);
  });

  // Quick search
  var searchInput = document.getElementById('quickSearch');
  if (searchInput) {
    searchInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && this.value.trim()) {
        window.location.href = '/tasks/?q=' + encodeURIComponent(this.value.trim());
      }
    });
  }

  // Keyboard shortcut Ctrl+K → focus search
  document.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      searchInput && searchInput.focus();
    }
  });

  // Progress bar animation on load
  document.querySelectorAll('.eo-progress-bar[data-value]').forEach(function (el) {
    var val = el.getAttribute('data-value');
    setTimeout(function () { el.style.width = val + '%'; }, 100);
  });

  // CSRF helper for AJAX
  window.getCsrfToken = function () {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
  };

  // Quick status update via AJAX
  document.querySelectorAll('.eo-quick-status').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var url = this.action;
      var data = new FormData(this);
      fetch(url, {
        method: 'POST',
        headers: { 'X-CSRFToken': window.getCsrfToken(), 'X-Requested-With': 'XMLHttpRequest' },
        body: data
      })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d.status === 'ok') {
          var badge = form.closest('.eo-task-card').querySelector('.eo-status');
          if (badge && d.new_status) badge.textContent = d.new_status;
          showToast('Status updated!', 'success');
        }
      });
    });
  });

  // Toast helper
  window.showToast = function (msg, type) {
    type = type || 'info';
    var icons = { success: 'check-circle', error: 'x-circle', warning: 'exclamation-triangle', info: 'info-circle' };
    var div = document.createElement('div');
    div.className = 'alert alert-' + type + ' alert-dismissible fade show eo-alert';
    div.style.cssText = 'position:fixed;bottom:24px;right:24px;z-index:9999;min-width:260px;';
    div.innerHTML = '<i class="bi bi-' + (icons[type] || 'info-circle') + ' me-2"></i>' + msg +
      '<button type="button" class="btn-close" data-bs-dismiss="alert"></button>';
    document.body.appendChild(div);
    setTimeout(function () { div.remove(); }, 4000);
  };

  // Confirm dialogs
  document.querySelectorAll('[data-confirm]').forEach(function (el) {
    el.addEventListener('click', function (e) {
      if (!confirm(this.getAttribute('data-confirm'))) e.preventDefault();
    });
  });

  // Tooltip initialization
  document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(function (el) {
    new bootstrap.Tooltip(el, { trigger: 'hover' });
  });
})();
