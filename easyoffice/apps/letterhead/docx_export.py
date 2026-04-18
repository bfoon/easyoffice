"""
apps/letterhead/docx_export.py

Server-side .docx export for LetterheadTemplate.

Delegates to the Node.js `generate_letterhead.js` script (ships alongside
this file) via a subprocess call so we get the full power of the `docx`
npm library without pulling a JS runtime into the Python layer permanently.

Django view:
    LetterheadDocxExportView  GET  /letterhead/api/templates/<uuid>/export/docx/

The generated file is:
  • A fully editable Word document.
  • Header  = letterhead branding (logo, company name, tagline, address,
              contact details) styled to the chosen template.
  • Footer  = contact / address strip matching the template colour.
  • Body    = pre-filled with a standard business letter scaffold
              (date, recipient block, salutation, body, closing) so the
              user can start typing immediately.

Requirements
────────────
  • Node.js ≥ 18 in PATH  (or set LETTERHEAD_NODE_BIN in settings.py)
  • The `docx` npm package installed globally:
        npm install -g docx
  • LETTERHEAD_SCRIPT_PATH in settings.py (defaults to a path relative
    to this file, so no configuration needed if you deploy everything
    together).

Optional settings
─────────────────
  LETTERHEAD_NODE_BIN      = '/usr/bin/node'  # override if node isn't in PATH
  LETTERHEAD_SCRIPT_PATH   = '/abs/path/to/generate_letterhead.js'
  LETTERHEAD_SCRIPT_TIMEOUT = 30              # seconds before giving up
"""

import json
import logging
import os
import subprocess
import tempfile
import uuid

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.generic import View

from .models import LetterheadTemplate
from .views import _require_letterhead_permission

logger = logging.getLogger(__name__)

# ── Locate the Node.js script ─────────────────────────────────────────────────

_HERE = os.path.dirname(os.path.abspath(__file__))

_DEFAULT_SCRIPT = os.path.join(_HERE, 'generate_letterhead.js')


def _node_bin() -> str:
    return getattr(settings, 'LETTERHEAD_NODE_BIN', 'node')


def _script_path() -> str:
    return getattr(settings, 'LETTERHEAD_SCRIPT_PATH', _DEFAULT_SCRIPT)


def _timeout() -> int:
    return int(getattr(settings, 'LETTERHEAD_SCRIPT_TIMEOUT', 30))


# ── View ──────────────────────────────────────────────────────────────────────

class LetterheadDocxExportView(LoginRequiredMixin, View):
    """
    GET /letterhead/api/templates/<uuid>/export/docx/

    Generates a fully editable .docx letterhead and streams it back as a
    file download.  Only authorised roles may export.
    """

    def get(self, request, pk):
        denied = _require_letterhead_permission(request.user)
        if denied:
            return denied

        template = get_object_or_404(LetterheadTemplate, pk=pk)
        return _generate_and_stream(template)


# ── Core generation logic ─────────────────────────────────────────────────────

def _generate_and_stream(template: LetterheadTemplate) -> HttpResponse:
    """
    Build the .docx via the Node script, read it back, delete the temp
    file, and return an HttpResponse with the binary content.
    """
    # Write to a unique temp file so concurrent exports don't collide.
    tmp_path = os.path.join(
        tempfile.gettempdir(),
        f'letterhead_{uuid.uuid4().hex}.docx',
    )

    cfg = _template_to_cfg(template, tmp_path)

    try:
        result = subprocess.run(
            [_node_bin(), _script_path()],
            input=json.dumps(cfg, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=_timeout(),
        )
    except FileNotFoundError:
        logger.error(
            'Node.js not found at "%s". '
            'Set LETTERHEAD_NODE_BIN in settings or install Node.js.',
            _node_bin(),
        )
        return JsonResponse(
            {'error': 'Node.js is not available on this server.  '
                      'Contact your system administrator.'},
            status=501,
        )
    except subprocess.TimeoutExpired:
        logger.error('generate_letterhead.js timed out for template %s', template.pk)
        return JsonResponse({'error': 'Export timed out.'}, status=504)

    if result.returncode != 0:
        logger.error(
            'generate_letterhead.js exited %d for template %s.\nstderr: %s',
            result.returncode, template.pk, result.stderr,
        )
        return JsonResponse(
            {'error': 'Document generation failed.', 'detail': result.stderr[:500]},
            status=500,
        )

    # Read the generated file and clean up.
    try:
        with open(tmp_path, 'rb') as fh:
            docx_bytes = fh.read()
    except OSError as exc:
        logger.error('Could not read generated docx at %s: %s', tmp_path, exc)
        return JsonResponse({'error': 'Could not read generated file.'}, status=500)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    safe_name = template.name.replace(' ', '_').replace('/', '-')
    filename  = f'letterhead_{safe_name}.docx'

    response = HttpResponse(
        docx_bytes,
        content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    response['Content-Length']      = len(docx_bytes)
    return response


# ── Config builder ────────────────────────────────────────────────────────────

def _template_to_cfg(template: LetterheadTemplate, output_path: str) -> dict:
    """Convert a LetterheadTemplate instance to the JSON config the JS script expects."""
    logo_path = None
    if template.logo:
        try:
            # .path raises ValueError if using a remote storage backend.
            logo_path = template.logo.path
        except (ValueError, NotImplementedError):
            # Remote storage (S3, GCS, etc.) — download to a temp file.
            logo_path = _download_logo_to_tmp(template)

    return {
        'output':          output_path,
        'template_key':    template.template_key,
        'company_name':    template.company_name,
        'tagline':         template.tagline or '',
        'address':         template.address or '',
        'contact_info':    template.contact_info or '',
        'color_primary':   template.color_primary,
        'color_secondary': template.color_secondary,
        'font_header':     template.font_header,
        'font_body':       template.font_body,
        'logo_path':       logo_path,
    }


def _download_logo_to_tmp(template: LetterheadTemplate) -> str | None:
    """
    For remote storage backends (S3, GCS, etc.) — download the logo to a
    local temp file so Node.js can read it.  Returns the temp path, or None
    on failure.  The caller must clean up this file when done.
    """
    try:
        ext  = os.path.splitext(template.logo.name)[1] or '.png'
        path = os.path.join(
            tempfile.gettempdir(),
            f'lh_logo_{uuid.uuid4().hex}{ext}',
        )
        with template.logo.open('rb') as src, open(path, 'wb') as dst:
            dst.write(src.read())
        return path
    except Exception as exc:
        logger.warning('Could not download logo for template %s: %s', template.pk, exc)
        return None
