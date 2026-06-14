"""
ZKTeco sync service.

Pipeline:
  1. pull_device()      -> read punches from a terminal into ZKPunchLog (raw, deduped)
  2. derive_attendance()-> fold raw punches into AttendanceRecord per staff/day

Both stages are independent: you can re-run derive_attendance() at any time
(e.g. after correcting a ZKUserMap) without touching the hardware.
"""
import logging
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from apps.hr.models import (
    AttendanceRecord,
    HRSetting,
    ZKDevice,
    ZKUserMap,
    ZKPunchLog,
)
from apps.hr.zk_client import ZKClient, ZKConnectionError

logger = logging.getLogger(__name__)


def _make_aware(dt):
    """Device timestamps are naive local time; attach the project timezone."""
    if dt is None:
        return None
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def pull_device(device, clear_after=None):
    """
    Read new punches from a single device into ZKPunchLog.

    Only punches strictly newer than device.last_punch_at are stored, so a
    re-run is cheap and idempotent. Returns a summary dict.
    """
    if clear_after is None:
        clear_after = device.clear_device_after_sync

    # Map enroll IDs -> staff for this device, resolved up front.
    user_map = {
        m.device_user_id: m.staff_id
        for m in ZKUserMap.objects.filter(device=device)
    }

    since = device.last_punch_at
    imported = 0
    skipped_old = 0
    unmapped = 0
    max_punch_time = since
    new_logs = []

    try:
        with ZKClient(device) as client:
            for punch in client.iter_attendance():
                ts = _make_aware(punch['timestamp'])
                if ts is None:
                    continue
                if since and ts <= since:
                    skipped_old += 1
                    continue

                device_user_id = punch['user_id']
                staff_id = user_map.get(device_user_id)
                if staff_id is None:
                    unmapped += 1

                new_logs.append(ZKPunchLog(
                    device=device,
                    device_user_id=device_user_id,
                    staff_id=staff_id,
                    punch_time=ts,
                    punch_type=punch.get('punch', 0) or 0,
                    status_code=punch.get('status', 0) or 0,
                    direction=_guess_direction(punch.get('punch', 0)),
                ))
                if max_punch_time is None or ts > max_punch_time:
                    max_punch_time = ts

            # Persist raw logs; ignore_conflicts handles the unique dedupe guard.
            if new_logs:
                created = ZKPunchLog.objects.bulk_create(new_logs, ignore_conflicts=True)
                imported = len(created)

            if clear_after and imported:
                client.clear_attendance()

    except ZKConnectionError as exc:
        device.last_sync_status = f'ERROR: {exc}'
        device.last_sync_at = timezone.now()
        device.save(update_fields=['last_sync_status', 'last_sync_at'])
        raise

    device.last_sync_at = timezone.now()
    device.last_punch_at = max_punch_time or device.last_punch_at
    device.last_sync_status = (
        f'OK: {imported} new, {skipped_old} old, {unmapped} unmapped'
    )
    device.save(update_fields=['last_sync_at', 'last_punch_at', 'last_sync_status'])

    summary = {
        'device': device.name,
        'imported': imported,
        'skipped_old': skipped_old,
        'unmapped': unmapped,
    }
    logger.info('ZK pull %s', summary)
    return summary


def _guess_direction(punch_code):
    """
    Map the device's punch code to a coarse direction.
    Most ZKTeco devices use 0=check-in, 1=check-out. Anything else is unknown
    and gets resolved by the first/last heuristic in derive_attendance().
    """
    if punch_code == 0:
        return ZKPunchLog.Direction.CHECK_IN
    if punch_code == 1:
        return ZKPunchLog.Direction.CHECK_OUT
    return ZKPunchLog.Direction.UNKNOWN


@transaction.atomic
def derive_attendance(start_date=None, end_date=None, staff_ids=None, marked_by=None):
    """
    Fold unprocessed ZKPunchLog rows into AttendanceRecord per staff/day.

    For each (staff, day) we take the earliest punch as check-in and the latest
    as check-out, then run the existing AttendanceRecord.apply_time_policy() so
    lateness, half-day and worked-hours rules stay identical to manual entry.

    Existing records are only updated when the device data is *better* (e.g. an
    earlier check-in or a later check-out) so manual HR corrections aren't
    silently clobbered.
    """
    setting = HRSetting.get_solo()

    logs = ZKPunchLog.objects.filter(processed=False, staff__isnull=False)
    if start_date:
        logs = logs.filter(punch_time__date__gte=start_date)
    if end_date:
        logs = logs.filter(punch_time__date__lte=end_date)
    if staff_ids:
        logs = logs.filter(staff_id__in=staff_ids)
    logs = logs.select_related('staff').order_by('punch_time')

    # Group punches by (staff_id, local date).
    buckets = {}
    processed_ids = []
    for log in logs:
        local_dt = timezone.localtime(log.punch_time)
        key = (log.staff_id, local_dt.date())
        bucket = buckets.setdefault(key, {'staff': log.staff, 'times': []})
        bucket['times'].append(local_dt.time())
        processed_ids.append(log.id)

    created = 0
    updated = 0

    for (staff_id, day), data in buckets.items():
        times = sorted(data['times'])
        first_punch = times[0]
        last_punch = times[-1] if len(times) > 1 else None

        record, was_created = AttendanceRecord.objects.get_or_create(
            staff_id=staff_id,
            date=day,
            defaults={
                'status': AttendanceRecord.Status.PRESENT,
                'check_in': first_punch,
                'check_out': last_punch,
                'marked_by': marked_by,
                'notes': 'Imported from biometric device.',
            },
        )

        if was_created:
            record.apply_time_policy(setting)
            record.save()
            created += 1
            continue

        # Existing record: merge conservatively. Take the earliest check-in and
        # latest check-out across manual + device values.
        changed = False
        if record.check_in is None or first_punch < record.check_in:
            record.check_in = first_punch
            changed = True
        candidate_out = last_punch or first_punch
        if record.check_out is None or candidate_out > record.check_out:
            record.check_out = candidate_out
            changed = True

        if changed:
            record.apply_time_policy(setting)
            record.save()
            updated += 1

    if processed_ids:
        ZKPunchLog.objects.filter(id__in=processed_ids).update(processed=True)

    summary = {'records_created': created, 'records_updated': updated, 'punches_processed': len(processed_ids)}
    logger.info('ZK derive %s', summary)
    return summary


def sync_device(device, marked_by=None):
    """Convenience: pull a device then derive attendance from what it returned."""
    pull_summary = pull_device(device)
    derive_summary = derive_attendance(marked_by=marked_by)
    return {**pull_summary, **derive_summary}


def sync_all_active_devices(marked_by=None):
    """Pull every active device, then derive once at the end."""
    results = []
    for device in ZKDevice.objects.filter(is_active=True):
        try:
            results.append(pull_device(device))
        except ZKConnectionError as exc:
            results.append({'device': device.name, 'error': str(exc)})
    derive_summary = derive_attendance(marked_by=marked_by)
    return {'devices': results, **derive_summary}


def import_users_as_maps(device, only_unmapped=True):
    """
    Read enrolled users from the terminal and return a list of suggestions
    HR can confirm in the UI. Auto-matches by exact name where possible.
    Does NOT create maps automatically — matching staff to enroll IDs is a
    decision HR should confirm.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    existing = set(
        ZKUserMap.objects.filter(device=device).values_list('device_user_id', flat=True)
    )
    suggestions = []
    with ZKClient(device) as client:
        for u in client.iter_users():
            if only_unmapped and u['user_id'] in existing:
                continue
            match = None
            name = u['name'].strip()
            if name:
                parts = name.split()
                qs = User.objects.filter(is_active=True)
                if len(parts) >= 2:
                    match = qs.filter(
                        first_name__iexact=parts[0], last_name__iexact=parts[-1]
                    ).first()
                if not match:
                    match = qs.filter(username__iexact=name.replace(' ', '.')).first()
            suggestions.append({
                'device_user_id': u['user_id'],
                'device_user_name': name,
                'suggested_staff_id': match.id if match else None,
                'suggested_staff_name': (match.get_full_name() if match else ''),
            })
    return suggestions
