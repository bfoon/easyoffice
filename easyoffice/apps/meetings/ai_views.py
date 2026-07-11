# apps/meetings/ai_views.py
#
# Advanced meetings features, kept in their own module so views.py stays
# untouched (urls.py imports both):
#
#   • MeetingCalendarFeedView   — JSON events feed for the calendar tab
#   • MeetingRecordingView      — list (GET) + upload (POST) audio recordings
#   • MeetingRecordingManageView— transcribe / rename / delete / poll status
#   • MinutesAutosaveView       — silent draft autosave from the editor
#   • MinutesAIDraftView        — turn transcripts into structured minutes
#
# Transcription and AI drafting are provider-pluggable via settings (with
# environment-variable fallbacks):
#
#   MEETINGS_TRANSCRIBE_PROVIDER = 'auto' | 'openai' | 'local'
#   OPENAI_API_KEY               — enables Whisper API transcription and
#                                  GPT minutes drafting
#   ANTHROPIC_API_KEY            — enables Claude minutes drafting
#   MEETINGS_AI_PROVIDER         = 'auto' | 'anthropic' | 'openai'
#
# 'local' transcription uses faster-whisper (pip install faster-whisper)
# or openai-whisper if installed — useful when internet access from the
# server is restricted. Everything degrades gracefully: recording and
# playback always work; the transcribe button explains what is missing
# if no provider is configured.

import os
import json
import tempfile
import threading
import traceback
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.generic import View
from django.db.models import Q

from apps.meetings.models import (
    Meeting, MeetingMinutes, MeetingRecording,
)
from apps.core.models import User


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────

def _json_err(message, status=400):
    return JsonResponse({'status': 'error', 'message': message}, status=status)


def _setting(name, default=''):
    return getattr(settings, name, '') or os.environ.get(name, '') or default


def _can_view(user, meeting):
    from apps.meetings.views import _can_view_meeting
    return _can_view_meeting(user, meeting)


def _can_contribute(user, meeting):
    """Organiser, superuser, or any internal attendee may record/transcribe."""
    if user.is_superuser or meeting.organizer_id == user.id:
        return True
    return meeting.attendees.filter(user=user).exists()


def _recording_json(rec):
    return {
        'id': str(rec.pk),
        'title': rec.display_title,
        'url': rec.audio_file.url if rec.audio_file else '',
        'mime_type': rec.mime_type,
        'size': rec.size_display,
        'duration': rec.duration_display,
        'duration_seconds': rec.duration_seconds,
        'recorded_by': rec.recorded_by.full_name if rec.recorded_by else '',
        'created': timezone.localtime(rec.created_at).strftime('%b %d, %Y · %H:%M'),
        'transcript_status': rec.transcript_status,
        'transcript_status_display': rec.get_transcript_status_display(),
        'status_color': rec.status_color,
        'transcript': rec.transcript if rec.transcript_status == 'completed' else '',
        'transcript_error': rec.transcript_error,
        'has_transcript': rec.transcript_status == 'completed' and bool(rec.transcript),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Calendar feed
# ─────────────────────────────────────────────────────────────────────────────

class MeetingCalendarFeedView(LoginRequiredMixin, View):
    """
    GET /meetings/calendar/feed/?start=YYYY-MM-DD&end=YYYY-MM-DD

    Returns the meetings visible to the current user inside the window,
    shaped for the month-grid calendar on the list page.
    """

    def get(self, request):
        try:
            start = datetime.strptime(request.GET.get('start', ''), '%Y-%m-%d')
            end = datetime.strptime(request.GET.get('end', ''), '%Y-%m-%d') + timedelta(days=1)
        except (ValueError, TypeError):
            return _json_err('Provide start and end as YYYY-MM-DD.')

        tz = timezone.get_current_timezone()
        start = timezone.make_aware(start, tz)
        end = timezone.make_aware(end, tz)

        user = request.user
        qs = (
            Meeting.objects.filter(
                Q(organizer=user) | Q(attendees__user=user) | Q(is_private=False)
            )
            .filter(start_datetime__lt=end, end_datetime__gte=start)
            .distinct()
            .select_related('organizer')
            .order_by('start_datetime')[:500]
        )

        my_rsvps = {}
        for att in user.meeting_invites.filter(meeting__in=qs):
            my_rsvps[att.meeting_id] = att.rsvp

        events = []
        for m in qs:
            local_start = timezone.localtime(m.start_datetime)
            local_end = timezone.localtime(m.end_datetime)
            events.append({
                'id': str(m.pk),
                'title': m.title,
                'date': local_start.strftime('%Y-%m-%d'),
                'start': local_start.strftime('%H:%M'),
                'end': local_end.strftime('%H:%M'),
                'start_iso': local_start.isoformat(),
                'color': m.type_color,
                'icon': m.type_icon,
                'type': m.get_meeting_type_display(),
                'status': m.status,
                'status_display': m.get_status_display(),
                'location': m.location,
                'virtual': bool(m.virtual_link),
                'url': reverse('meeting_detail', kwargs={'pk': m.pk}),
                'is_organizer': m.organizer_id == user.id,
                'my_rsvp': my_rsvps.get(m.pk, ''),
                'has_minutes': hasattr(m, 'minutes'),
            })

        return JsonResponse({'status': 'ok', 'events': events})


# ─────────────────────────────────────────────────────────────────────────────
# 2. Recordings — list + upload
# ─────────────────────────────────────────────────────────────────────────────

MAX_RECORDING_BYTES = 200 * 1024 * 1024   # 200 MB
ALLOWED_AUDIO_PREFIXES = ('audio/', 'video/webm')  # MediaRecorder may report video/webm


class MeetingRecordingView(LoginRequiredMixin, View):
    """
    GET  /meetings/<pk>/recordings/          → JSON list
    POST /meetings/<pk>/recordings/          → upload one recording
         fields: audio (file), title, duration_seconds
    """

    def get(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_view(request.user, meeting):
            return _json_err('Forbidden', status=403)

        recs = meeting.recordings.select_related('recorded_by').all()
        return JsonResponse({
            'status': 'ok',
            'can_contribute': _can_contribute(request.user, meeting),
            'transcribe_available': bool(_transcribe_provider_available()[0]),
            'ai_available': bool(_ai_provider_available()[0]),
            'recordings': [_recording_json(r) for r in recs],
        })

    def post(self, request, pk):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_contribute(request.user, meeting):
            return _json_err('Only the organiser or attendees can add recordings.', status=403)

        uploaded = request.FILES.get('audio')
        if not uploaded:
            return _json_err('No audio received.')
        if uploaded.size > MAX_RECORDING_BYTES:
            return _json_err('Recording too large (max 200 MB).')

        mime = (getattr(uploaded, 'content_type', '') or '').lower()
        if mime and not mime.startswith(ALLOWED_AUDIO_PREFIXES):
            return _json_err('Only audio recordings are accepted.')

        try:
            duration = max(0, int(float(request.POST.get('duration_seconds') or 0)))
        except (ValueError, TypeError):
            duration = 0

        title = (request.POST.get('title') or '').strip()[:200]

        ext = {
            'audio/webm': 'webm', 'video/webm': 'webm',
            'audio/ogg': 'ogg', 'audio/mpeg': 'mp3', 'audio/mp3': 'mp3',
            'audio/mp4': 'm4a', 'audio/x-m4a': 'm4a', 'audio/wav': 'wav',
            'audio/x-wav': 'wav', 'audio/aac': 'aac',
        }.get(mime.split(';')[0], 'webm')

        fname = f'recording-{timezone.now():%Y%m%d-%H%M%S}.{ext}'

        rec = MeetingRecording.objects.create(
            meeting=meeting,
            recorded_by=request.user,
            title=title,
            mime_type=mime,
            file_size=uploaded.size,
            duration_seconds=duration,
        )
        rec.audio_file.save(fname, uploaded, save=True)

        return JsonResponse({'status': 'ok',
                             'message': 'Recording saved.',
                             'recording': _recording_json(rec)})


# ─────────────────────────────────────────────────────────────────────────────
# 3. Recording management — transcribe / status / rename / delete
# ─────────────────────────────────────────────────────────────────────────────

class MeetingRecordingManageView(LoginRequiredMixin, View):
    """
    GET  /meetings/<pk>/recordings/<rid>/     → status + transcript JSON
    POST /meetings/<pk>/recordings/<rid>/     → action=transcribe | rename | delete
    """

    def _resolve(self, request, pk, rid):
        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_view(request.user, meeting):
            return None, None, _json_err('Forbidden', status=403)
        rec = meeting.recordings.filter(pk=rid).first()
        if not rec:
            return None, None, _json_err('Recording not found.', status=404)
        return meeting, rec, None

    def get(self, request, pk, rid):
        meeting, rec, err = self._resolve(request, pk, rid)
        if err:
            return err
        return JsonResponse({'status': 'ok', 'recording': _recording_json(rec)})

    def post(self, request, pk, rid):
        meeting, rec, err = self._resolve(request, pk, rid)
        if err:
            return err

        action = (request.POST.get('action') or '').strip()

        if action == 'transcribe':
            if not _can_contribute(request.user, meeting):
                return _json_err('Only the organiser or attendees can transcribe.', status=403)
            if rec.transcript_status in ('pending', 'processing'):
                return _json_err('Transcription is already running for this recording.')

            provider, why_not = _transcribe_provider_available()
            if not provider:
                return _json_err(f'Transcription is not configured: {why_not}')

            rec.transcript_status = MeetingRecording.TranscriptStatus.PENDING
            rec.transcript_error = ''
            rec.save(update_fields=['transcript_status', 'transcript_error', 'updated_at'])

            t = threading.Thread(target=_transcription_worker,
                                 args=(str(rec.pk),), daemon=True)
            t.start()

            return JsonResponse({'status': 'ok',
                                 'message': 'Transcription started — this can take a few minutes for long recordings.',
                                 'recording': _recording_json(rec)})

        if action == 'rename':
            if not (request.user.is_superuser
                    or meeting.organizer_id == request.user.id
                    or rec.recorded_by_id == request.user.id):
                return _json_err('Forbidden', status=403)
            rec.title = (request.POST.get('title') or '').strip()[:200]
            rec.save(update_fields=['title', 'updated_at'])
            return JsonResponse({'status': 'ok', 'recording': _recording_json(rec)})

        if action == 'delete':
            if not (request.user.is_superuser
                    or meeting.organizer_id == request.user.id
                    or rec.recorded_by_id == request.user.id):
                return _json_err('Only the uploader or organiser can delete a recording.', status=403)
            try:
                rec.audio_file.delete(save=False)
            except Exception:
                pass
            rec.delete()
            return JsonResponse({'status': 'ok', 'message': 'Recording deleted.'})

        return _json_err('Unknown action')


# ─────────────────────────────────────────────────────────────────────────────
# 4. Transcription engine (background thread)
# ─────────────────────────────────────────────────────────────────────────────

def _transcribe_provider_available():
    """Return (provider_name or None, reason-if-none)."""
    pref = (_setting('MEETINGS_TRANSCRIBE_PROVIDER', 'auto') or 'auto').lower()

    def openai_ok():
        return bool(_setting('OPENAI_API_KEY'))

    def local_ok():
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            try:
                import whisper  # noqa: F401
                return True
            except ImportError:
                return False

    if pref == 'openai':
        if openai_ok():
            return 'openai', ''
        return None, 'set OPENAI_API_KEY in settings or the environment.'
    if pref == 'local':
        if local_ok():
            return 'local', ''
        return None, 'install faster-whisper (pip install faster-whisper) on the server.'
    # auto
    if openai_ok():
        return 'openai', ''
    if local_ok():
        return 'local', ''
    return None, ('set OPENAI_API_KEY for Whisper API transcription, or install '
                  'faster-whisper for on-server transcription.')


def _transcription_worker(recording_id):
    """Runs in a daemon thread. Downloads the audio to a temp file,
    transcribes it with the configured provider, and stores the result."""
    try:
        rec = MeetingRecording.objects.get(pk=recording_id)
    except MeetingRecording.DoesNotExist:
        return

    rec.transcript_status = MeetingRecording.TranscriptStatus.PROCESSING
    rec.save(update_fields=['transcript_status', 'updated_at'])

    tmp_path = None
    try:
        suffix = os.path.splitext(rec.audio_file.name or '')[1] or '.webm'
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
            with rec.audio_file.open('rb') as fh:
                for chunk in fh.chunks() if hasattr(fh, 'chunks') else iter(lambda: fh.read(1024 * 512), b''):
                    tmp.write(chunk)

        provider, _why = _transcribe_provider_available()
        if provider == 'openai':
            text, lang = _transcribe_openai(tmp_path)
        elif provider == 'local':
            text, lang = _transcribe_local(tmp_path)
        else:
            raise RuntimeError('No transcription provider configured.')

        rec.transcript = (text or '').strip()
        rec.transcript_language = (lang or '')[:10]
        rec.transcript_status = MeetingRecording.TranscriptStatus.COMPLETED
        rec.transcript_error = ''
        rec.transcribed_at = timezone.now()
        rec.save(update_fields=['transcript', 'transcript_language',
                                'transcript_status', 'transcript_error',
                                'transcribed_at', 'updated_at'])
    except Exception as e:
        traceback.print_exc()
        rec.transcript_status = MeetingRecording.TranscriptStatus.FAILED
        rec.transcript_error = str(e)[:500]
        rec.save(update_fields=['transcript_status', 'transcript_error', 'updated_at'])
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _transcribe_openai(path):
    """Whisper API — multipart upload, returns (text, language)."""
    import requests
    api_key = _setting('OPENAI_API_KEY')
    with open(path, 'rb') as fh:
        resp = requests.post(
            'https://api.openai.com/v1/audio/transcriptions',
            headers={'Authorization': f'Bearer {api_key}'},
            files={'file': (os.path.basename(path), fh)},
            data={'model': _setting('MEETINGS_WHISPER_MODEL', 'whisper-1'),
                  'response_format': 'verbose_json'},
            timeout=600,
        )
    if resp.status_code != 200:
        raise RuntimeError(f'Whisper API error {resp.status_code}: {resp.text[:200]}')
    data = resp.json()
    return data.get('text', ''), data.get('language', '')


def _transcribe_local(path):
    """On-server transcription — faster-whisper preferred, whisper fallback."""
    model_size = _setting('MEETINGS_LOCAL_WHISPER_MODEL', 'base')
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(model_size, device='cpu', compute_type='int8')
        segments, info = model.transcribe(path)
        text = ' '.join(seg.text.strip() for seg in segments)
        return text, getattr(info, 'language', '')
    except ImportError:
        pass
    import whisper
    model = whisper.load_model(model_size)
    result = model.transcribe(path)
    return result.get('text', ''), result.get('language', '')


# ─────────────────────────────────────────────────────────────────────────────
# 5. Minutes autosave
# ─────────────────────────────────────────────────────────────────────────────

class MinutesAutosaveView(LoginRequiredMixin, View):
    """
    POST /meetings/<pk>/minutes/autosave/
         fields: content, decisions

    Silent draft save from the editor (debounced client-side + Ctrl+S).
    Only touches the two rich-text fields — action items still go through
    the normal Save so the row-rebuild logic stays in one place.
    """

    def post(self, request, pk):
        from apps.meetings.views import _sanitise_minutes_html

        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_view(request.user, meeting):
            return _json_err('Forbidden', status=403)

        minutes, _created = MeetingMinutes.objects.get_or_create(
            meeting=meeting, defaults={'author': request.user}
        )
        if not (minutes.is_editable or request.user.is_superuser):
            return _json_err('Minutes are locked and can no longer be edited.')

        minutes.content = _sanitise_minutes_html(request.POST.get('content', ''))
        minutes.decisions = _sanitise_minutes_html(request.POST.get('decisions', ''))
        minutes.author = request.user
        minutes.save(update_fields=['content', 'decisions', 'author', 'updated_at'])

        return JsonResponse({
            'status': 'ok',
            'saved_at': timezone.localtime(minutes.updated_at).strftime('%H:%M:%S'),
        })


# ─────────────────────────────────────────────────────────────────────────────
# 6. AI minutes drafting
# ─────────────────────────────────────────────────────────────────────────────

def _ai_provider_available():
    pref = (_setting('MEETINGS_AI_PROVIDER', 'auto') or 'auto').lower()
    if pref == 'anthropic':
        return ('anthropic', '') if _setting('ANTHROPIC_API_KEY') else \
               (None, 'set ANTHROPIC_API_KEY in settings or the environment.')
    if pref == 'openai':
        return ('openai', '') if _setting('OPENAI_API_KEY') else \
               (None, 'set OPENAI_API_KEY in settings or the environment.')
    if _setting('ANTHROPIC_API_KEY'):
        return 'anthropic', ''
    if _setting('OPENAI_API_KEY'):
        return 'openai', ''
    return None, 'set ANTHROPIC_API_KEY or OPENAI_API_KEY to enable AI minutes drafting.'


_AI_SYSTEM_PROMPT = (
    'You are a professional minute-taker for a UN-style office. You are given '
    'a meeting transcript (and optionally the agenda). Produce formal, neutral, '
    'well-structured meeting minutes. Respond with ONLY a JSON object — no '
    'markdown fences, no commentary — with exactly these keys:\n'
    '  "summary_html": string — the minutes body as simple HTML using only '
    '<h2>, <h3>, <p>, <ul>, <ol>, <li>, <b>, <i> tags. Organise by agenda '
    'topic where possible. Third person, past tense, concise.\n'
    '  "decisions_html": string — key decisions as an HTML <ul> list, or "" '
    'if none were made.\n'
    '  "action_items": array of objects with keys "description" (string, '
    'imperative), "owner_name" (string, the responsible person\'s name as '
    'heard, or ""), "due_date" (string YYYY-MM-DD if a deadline was stated, '
    'else "").\n'
    'Do not invent decisions or action items that are not supported by the '
    'transcript.'
)


def _ai_chat(prompt_text):
    """Send the drafting prompt to the configured provider, return raw text."""
    import requests
    provider, why_not = _ai_provider_available()
    if not provider:
        raise RuntimeError(f'AI drafting is not configured: {why_not}')

    if provider == 'anthropic':
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': _setting('ANTHROPIC_API_KEY'),
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': _setting('MEETINGS_ANTHROPIC_MODEL', 'claude-sonnet-4-6'),
                'max_tokens': 4000,
                'system': _AI_SYSTEM_PROMPT,
                'messages': [{'role': 'user', 'content': prompt_text}],
            },
            timeout=180,
        )
        if resp.status_code != 200:
            raise RuntimeError(f'Anthropic API error {resp.status_code}: {resp.text[:200]}')
        data = resp.json()
        return ''.join(b.get('text', '') for b in data.get('content', [])
                       if b.get('type') == 'text')

    # openai
    resp = requests.post(
        'https://api.openai.com/v1/chat/completions',
        headers={
            'Authorization': f'Bearer {_setting("OPENAI_API_KEY")}',
            'Content-Type': 'application/json',
        },
        json={
            'model': _setting('MEETINGS_OPENAI_MODEL', 'gpt-4o-mini'),
            'messages': [
                {'role': 'system', 'content': _AI_SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt_text},
            ],
            'temperature': 0.2,
        },
        timeout=180,
    )
    if resp.status_code != 200:
        raise RuntimeError(f'OpenAI API error {resp.status_code}: {resp.text[:200]}')
    return resp.json()['choices'][0]['message']['content']


def _strip_html(text):
    import re as _re
    return _re.sub(r'<[^>]+>', ' ', text or '').strip()


class MinutesAIDraftView(LoginRequiredMixin, View):
    """
    POST /meetings/<pk>/minutes/ai-draft/
         optional: recording_id — draft from one recording; otherwise all
                   completed transcripts for the meeting are combined.

    Returns sanitised summary/decisions HTML plus structured action items
    with owner names resolved against staff where possible. Nothing is
    written to the minutes — the minute-taker reviews and inserts.
    """

    MAX_TRANSCRIPT_CHARS = 60000

    def post(self, request, pk):
        from apps.meetings.views import _sanitise_minutes_html

        meeting = get_object_or_404(Meeting, pk=pk)
        if not _can_contribute(request.user, meeting):
            return _json_err('Only the organiser or attendees can generate AI minutes.', status=403)

        provider, why_not = _ai_provider_available()
        if not provider:
            return _json_err(f'AI drafting is not configured: {why_not}')

        rid = (request.POST.get('recording_id') or '').strip()
        if rid:
            recs = list(meeting.recordings.filter(
                pk=rid, transcript_status='completed'))
        else:
            recs = list(meeting.recordings.filter(transcript_status='completed'))

        transcripts = [r.transcript for r in recs if r.transcript]
        if not transcripts:
            return _json_err('No completed transcript found. Transcribe a recording first.')

        transcript_text = '\n\n---\n\n'.join(transcripts)
        if len(transcript_text) > self.MAX_TRANSCRIPT_CHARS:
            transcript_text = transcript_text[:self.MAX_TRANSCRIPT_CHARS] + '\n[transcript truncated]'

        attendee_names = [a.user.full_name for a in
                          meeting.attendees.select_related('user').all()]
        attendee_names += [e.full_name for e in meeting.external_attendees.all()]

        prompt = (
            f'MEETING: {meeting.title}\n'
            f'TYPE: {meeting.get_meeting_type_display()}\n'
            f'DATE: {timezone.localtime(meeting.start_datetime):%A, %B %d, %Y %H:%M}\n'
            f'ORGANISER: {meeting.organizer.full_name}\n'
            f'ATTENDEES: {", ".join(attendee_names) or "unknown"}\n'
        )
        agenda_text = _strip_html(meeting.agenda)
        if agenda_text:
            prompt += f'\nAGENDA:\n{agenda_text[:3000]}\n'
        prompt += f'\nTRANSCRIPT:\n{transcript_text}'

        try:
            raw = _ai_chat(prompt)
        except RuntimeError as e:
            return _json_err(str(e))
        except Exception as e:
            return _json_err(f'AI request failed: {e}')

        # Parse the JSON payload (strip accidental fences defensively).
        cleaned = raw.strip()
        if cleaned.startswith('```'):
            cleaned = cleaned.strip('`')
            if cleaned.lower().startswith('json'):
                cleaned = cleaned[4:]
        try:
            payload = json.loads(cleaned)
        except Exception:
            # Last-ditch: find the outermost braces
            try:
                payload = json.loads(cleaned[cleaned.index('{'):cleaned.rindex('}') + 1])
            except Exception:
                return _json_err('The AI response could not be parsed. Please try again.')

        summary_html = _sanitise_minutes_html(str(payload.get('summary_html', '')))
        decisions_html = _sanitise_minutes_html(str(payload.get('decisions_html', '')))

        # Resolve owner names → staff ids where we can.
        staff = {u.full_name.lower(): str(u.pk)
                 for u in User.objects.filter(is_active=True)}
        items = []
        for it in (payload.get('action_items') or [])[:40]:
            desc = str(it.get('description', '')).strip()[:500]
            if not desc:
                continue
            owner_name = str(it.get('owner_name', '')).strip()
            owner_id = ''
            if owner_name:
                key = owner_name.lower()
                owner_id = staff.get(key, '')
                if not owner_id:
                    for full, uid in staff.items():
                        if key in full or full in key:
                            owner_id = uid
                            break
            due = str(it.get('due_date', '')).strip()
            try:
                if due:
                    datetime.strptime(due, '%Y-%m-%d')
            except ValueError:
                due = ''
            items.append({'description': desc, 'owner_name': owner_name,
                          'owner_id': owner_id, 'due_date': due})

        return JsonResponse({
            'status': 'ok',
            'summary_html': summary_html,
            'decisions_html': decisions_html,
            'action_items': items,
            'source_recordings': len(recs),
        })
