"""
Geolocation helpers for the surveys feature.

Three utilities:
    parse_geo_answer(value_str) -> dict | None
        Parses a SurveyAnswer.value that was written by a geolocation question.

    lookup_ip_location(ip_address) -> dict | None
        Server-side IP → approximate location lookup using ipapi.co (free tier)
        with ip-api.com fallback. Cached per-IP for 5 minutes.

    reverse_geocode(lat, lng) -> str | None
        Converts coordinates to a human-readable address using OpenStreetMap's
        Nominatim service. Cached for 1 hour per rounded coordinate pair.
        Respects Nominatim's 1 req/sec rate limit via a simple semaphore.
"""
import json
import time
import logging
import threading

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Nominatim politely requires a descriptive User-Agent identifying the app
# per their usage policy: https://operations.osmfoundation.org/policies/nominatim/
_USER_AGENT = 'EasyOffice-Surveys/1.0 (https://easyoffice.local)'

# Simple single-process throttle for Nominatim (1 req/sec policy)
_nominatim_lock = threading.Lock()
_nominatim_last_call = [0.0]
_NOMINATIM_MIN_INTERVAL = 1.1  # seconds


def parse_geo_answer(value):
    """
    Decode a geolocation answer value into a dict.

    Accepts:
        - a JSON string as written by the geolocation question
        - legacy "lat,lng" plain text (parsed if it looks like floats)
        - None / empty (returns None)

    Returns a dict with keys: lat, lng, accuracy_m, source, address,
    captured_at — or None if unparseable.
    """
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None

    # Try JSON first
    if s.startswith('{'):
        try:
            data = json.loads(s)
            if isinstance(data, dict) and 'lat' in data and 'lng' in data:
                return {
                    'lat': float(data.get('lat')),
                    'lng': float(data.get('lng')),
                    'accuracy_m': float(data.get('accuracy_m') or 0),
                    'source': data.get('source', 'gps'),
                    'address': data.get('address', ''),
                    'captured_at': data.get('captured_at', ''),
                }
        except (ValueError, TypeError):
            return None

    # Fallback: plain "lat,lng" legacy format
    if ',' in s:
        parts = [p.strip() for p in s.split(',')]
        if len(parts) >= 2:
            try:
                return {
                    'lat': float(parts[0]),
                    'lng': float(parts[1]),
                    'accuracy_m': 0,
                    'source': 'unknown',
                    'address': '',
                    'captured_at': '',
                }
            except ValueError:
                return None
    return None


def format_geo_display(geo):
    """Produce a short display string for a parsed geo dict."""
    if not geo:
        return ''
    src = geo.get('source', '').upper()
    acc = geo.get('accuracy_m') or 0
    lat = geo.get('lat')
    lng = geo.get('lng')
    addr = geo.get('address', '')
    core = f'{lat:.5f}, {lng:.5f}'
    tag_bits = []
    if acc:
        tag_bits.append(f'±{acc:.0f} m')
    if src:
        tag_bits.append(src)
    tag = f' ({", ".join(tag_bits)})' if tag_bits else ''
    return f'{addr + " — " if addr else ""}{core}{tag}'


# ── IP lookup ────────────────────────────────────────────────────────────────

def lookup_ip_location(ip_address, timeout=4):
    """
    Look up approximate location for an IP address. Returns a dict:
        {lat, lng, accuracy_m, source: 'ip', address, city, country}
    or None on failure.
    """
    if not ip_address:
        return None
    # Don't bother for private / localhost IPs
    if ip_address.startswith(('10.', '127.', '192.168.', '172.', '::1')):
        return None

    cache_key = f'geo:iplocate:{ip_address}'
    cached = cache.get(cache_key)
    if cached:
        return cached

    # ── Primary: ipapi.co ────────────────────────────────────────────────────
    try:
        r = requests.get(
            f'https://ipapi.co/{ip_address}/json/',
            headers={'User-Agent': _USER_AGENT},
            timeout=timeout,
        )
        if r.status_code == 200:
            j = r.json()
            if j.get('latitude') and j.get('longitude'):
                city = j.get('city', '')
                country = j.get('country_name', '')
                addr_bits = [b for b in [city, j.get('region', ''), country] if b]
                result = {
                    'lat':        float(j['latitude']),
                    'lng':        float(j['longitude']),
                    'accuracy_m': 20000.0,     # IP geoloc is ~20km accuracy
                    'source':     'ip',
                    'address':    ', '.join(addr_bits),
                    'city':       city,
                    'country':    country,
                }
                cache.set(cache_key, result, 300)  # 5 min
                return result
    except Exception as e:
        logger.info('ipapi.co failed for %s: %s', ip_address, e)

    # ── Fallback: ip-api.com ─────────────────────────────────────────────────
    try:
        r = requests.get(
            f'http://ip-api.com/json/{ip_address}?fields=status,country,regionName,city,lat,lon,message',
            headers={'User-Agent': _USER_AGENT},
            timeout=timeout,
        )
        if r.status_code == 200:
            j = r.json()
            if j.get('status') == 'success' and j.get('lat') and j.get('lon'):
                city = j.get('city', '')
                country = j.get('country', '')
                addr_bits = [b for b in [city, j.get('regionName', ''), country] if b]
                result = {
                    'lat':        float(j['lat']),
                    'lng':        float(j['lon']),
                    'accuracy_m': 20000.0,
                    'source':     'ip',
                    'address':    ', '.join(addr_bits),
                    'city':       city,
                    'country':    country,
                }
                cache.set(cache_key, result, 300)
                return result
    except Exception as e:
        logger.info('ip-api.com failed for %s: %s', ip_address, e)

    return None


# ── Reverse geocode ──────────────────────────────────────────────────────────

def reverse_geocode(lat, lng, timeout=4):
    """
    Coordinates → human-readable address via Nominatim (OSM).
    Returns the address string or None.
    Cached for 1h per ~100-meter coordinate grid cell.
    """
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return None

    # Round to 4 dp (~11m) for cache key — spares repeated lookups for the same pin
    key = f'geo:revgeo:{lat:.4f},{lng:.4f}'
    cached = cache.get(key)
    if cached is not None:
        return cached

    # Politely throttle to Nominatim's 1 req/sec limit
    with _nominatim_lock:
        delta = time.time() - _nominatim_last_call[0]
        if delta < _NOMINATIM_MIN_INTERVAL:
            time.sleep(_NOMINATIM_MIN_INTERVAL - delta)
        _nominatim_last_call[0] = time.time()

    try:
        r = requests.get(
            'https://nominatim.openstreetmap.org/reverse',
            params={'lat': lat, 'lon': lng, 'format': 'json', 'zoom': 16, 'addressdetails': 1},
            headers={'User-Agent': _USER_AGENT, 'Accept-Language': 'en'},
            timeout=timeout,
        )
        if r.status_code == 200:
            j = r.json()
            addr = j.get('display_name') or ''
            if addr:
                cache.set(key, addr, 3600)  # 1 hour
                return addr
    except Exception as e:
        logger.info('Nominatim reverse-geocode failed for %s,%s: %s', lat, lng, e)

    # Cache a negative result briefly so we don't hammer Nominatim on a bad coord
    cache.set(key, '', 60)
    return None


# ── Plus Code / Open Location Code conversion ─────────────────────────────────

# Pure-Python OLC implementation (no extra pip package needed)
# Spec: https://github.com/google/open-location-code

_OLC_ALPHABET = '23456789CFGHJMPQRVWX'
_OLC_PAIR_LENGTH = 10
_OLC_GRID_COLS = 4
_OLC_GRID_ROWS = 5
_LAT_MAX = 90.0
_LNG_MAX = 180.0


def _olc_is_full(code: str) -> bool:
    code = code.strip().upper()
    plus = code.find('+')
    if plus < 0:
        return False
    if plus != 8 and (plus < 8 and len(code) < plus + 3):
        pass
    if any(c not in _OLC_ALPHABET and c != '+' and c != '0' for c in code.replace('0', '')):
        return False
    return '+' in code and code.index('+') >= 2


def _olc_is_short(code: str) -> bool:
    code = code.strip().upper()
    return '+' in code and code.index('+') < 8


def _olc_decode(code: str):
    """
    Decode a full Plus Code to (lat_center, lng_center).
    Returns None if invalid.
    """
    code = code.strip().upper().replace('+', '').replace('0', '')
    if not code:
        return None
    lat, lng = -_LAT_MAX, -_LNG_MAX
    lat_res, lng_res = _LAT_MAX * 2, _LNG_MAX * 2
    try:
        for i in range(0, min(len(code), _OLC_PAIR_LENGTH), 2):
            lat_res /= len(_OLC_ALPHABET)
            lng_res /= len(_OLC_ALPHABET)
            lat += _OLC_ALPHABET.index(code[i])     * lat_res
            lng += _OLC_ALPHABET.index(code[i + 1]) * lng_res
        if len(code) > _OLC_PAIR_LENGTH:
            lat_res /= _OLC_GRID_ROWS
            lng_res /= _OLC_GRID_COLS
            for ch in code[_OLC_PAIR_LENGTH:]:
                row = _OLC_ALPHABET.index(ch) // _OLC_GRID_COLS
                col = _OLC_ALPHABET.index(ch) %  _OLC_GRID_COLS
                lat += row * lat_res
                lng += col * lng_res
        return lat + lat_res / 2, lng + lng_res / 2
    except (ValueError, IndexError):
        return None


def _olc_recover(short_code: str, ref_lat: float, ref_lng: float):
    """
    Recover a short Plus Code to full using a reference lat/lng, then decode.
    Returns (lat, lng) or None.
    """
    short_code = short_code.strip().upper()
    plus_pos = short_code.index('+')
    padding = 8 - plus_pos   # chars to prepend
    ref_lat = max(-90.0, min(90.0, ref_lat))
    ref_lng = max(-180.0, min(180.0, ref_lng))

    lat_res = 400.0 / (len(_OLC_ALPHABET) ** ((plus_pos // 2) - 1 + 1))
    lng_res = lat_res

    ref_lat_f = (ref_lat + _LAT_MAX) // lat_res * lat_res - _LAT_MAX
    ref_lng_f = (ref_lng + _LNG_MAX) // lng_res * lng_res - _LNG_MAX

    prefix_lat = int((ref_lat_f + _LAT_MAX) / (lat_res * 20))
    prefix_lng = int((ref_lng_f + _LNG_MAX) / (lng_res * 20))

    full_code = ''
    for pos in range(4, plus_pos, 2):
        d = len(_OLC_ALPHABET)
        full_code += _OLC_ALPHABET[prefix_lat // (d ** ((pos // 2) - 1)) % d]
        full_code += _OLC_ALPHABET[prefix_lng // (d ** ((pos // 2) - 1)) % d]
    full_code += short_code
    return _olc_decode(full_code)


# Project reference coordinates for short-code recovery
# (Gambia / Banjul as default — matches OLC_REF in location_map.html)
_DEFAULT_REF_LAT = 13.4549
_DEFAULT_REF_LNG = -16.5790


def plus_code_to_latlng(code: str, ref_lat: float = _DEFAULT_REF_LAT,
                         ref_lng: float = _DEFAULT_REF_LNG):
    """
    Convert a Plus Code (full or short, optionally with a locality name appended)
    to (latitude, longitude).  Returns (lat, lng) floats or (None, None).

    Examples:
        plus_code_to_latlng("6FG22222+22")          # full
        plus_code_to_latlng("C7PM+F8G Banjul")      # short + locality
        plus_code_to_latlng("C7PM+F8G", 13.45, -16.58)
    """
    if not code:
        return None, None
    # Strip a locality name after the code (e.g. "C7PM+F8G Banjul")
    raw = str(code).strip().upper()
    parts = raw.split()
    raw_code = parts[0]

    try:
        if _olc_is_full(raw_code):
            result = _olc_decode(raw_code)
        elif _olc_is_short(raw_code):
            result = _olc_recover(raw_code, ref_lat, ref_lng)
        else:
            return None, None
        if result:
            return round(result[0], 7), round(result[1], 7)
    except Exception as exc:
        logger.debug('plus_code_to_latlng failed for %r: %s', code, exc)
    return None, None


def is_plus_code(value: str) -> bool:
    """Return True if the string looks like a Plus Code."""
    if not value:
        return False
    v = str(value).strip().upper().split()[0]
    return _olc_is_full(v) or _olc_is_short(v)


# ── Export helper — flatten geo answer to dict of columns ────────────────────

def geo_answer_to_columns(value) -> dict:
    """
    Convert a raw geo answer value (JSON string or legacy lat,lng) into a flat
    dict ready for CSV / XLSX export.

    Returns columns:
        latitude, longitude, accuracy_m, source, address, captured_at
    All values are plain Python scalars (str / float / int).
    Empty string for missing fields so every row has the same keys.
    """
    geo = parse_geo_answer(value)
    if not geo:
        return {
            'latitude':    '',
            'longitude':   '',
            'accuracy_m':  '',
            'source':      '',
            'address':     '',
            'captured_at': '',
        }
    return {
        'latitude':    geo.get('lat',         ''),
        'longitude':   geo.get('lng',         ''),
        'accuracy_m':  geo.get('accuracy_m',  ''),
        'source':      geo.get('source',      ''),
        'address':     geo.get('address',     ''),
        'captured_at': geo.get('captured_at', ''),
    }