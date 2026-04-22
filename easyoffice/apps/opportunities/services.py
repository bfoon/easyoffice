import hashlib
import logging
import re
from datetime import datetime
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from django.db.models import Q
from django.utils import timezone

from apps.core.models import CoreNotification, User
from apps.opportunities.models import (
    OpportunitySource,
    OpportunityKeyword,
    OpportunityMatch,
)

logger = logging.getLogger(__name__)

ADMIN_GROUPS = ['CEO', 'Admin', 'Office Manager']


def make_fingerprint(source_id, title, url, reference_no=''):
    raw = f"{source_id}|{reference_no}|{title}|{url}".strip().lower()
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def get_admin_recipients():
    return User.objects.filter(
        Q(is_superuser=True) | Q(groups__name__in=ADMIN_GROUPS),
        is_active=True
    ).distinct()


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value)).strip()


def normalize_reference(value):
    if not value:
        return ''
    return re.sub(r'\s+', '', str(value)).upper().strip()


def find_matching_keyword(text):
    if not text:
        return None

    lowered = text.lower()
    for keyword in OpportunityKeyword.objects.filter(is_active=True):
        if keyword.keyword.lower() in lowered:
            return keyword
    return None


def parse_undp_datetime(text):
    if not text:
        return None

    cleaned = re.sub(r'\s*\(.*?\)\s*', '', str(text)).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)

    formats = [
        '%d-%b-%y %I:%M %p',
        '%d-%b-%Y %I:%M %p',
        '%d-%b-%y',
        '%d-%b-%Y',
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(cleaned, fmt)
            return timezone.make_aware(dt, timezone.get_current_timezone())
        except Exception:
            continue
    return None


def split_office_country(value):
    value = clean_text(value)
    if not value:
        return '', ''

    parts = value.split('/', 1)
    if len(parts) == 2:
        return clean_text(parts[0]), clean_text(parts[1])
    return value, ''


def parse_compound_procurement_text(text):
    """
    Parse strings like:

    Procurement of Various ICT Equipment: Laptop, Tablets, Camera and accessories
    Ref No UNDP-PHL-00886,2
    UNDP Office/Country UNDP-PHL/PHILIPPINES
    Process RFQ - Request for quotation
    Deadline 20-Apr-26 10:00 AM (New York time)
    Posted 23-Mar-26
    """
    result = {
        'title': '',
        'reference_no': '',
        'office_country_raw': '',
        'office': '',
        'country': '',
        'procurement_process': '',
        'deadline_text': '',
        'posted_text': '',
        'deadline': None,
        'posted_date': None,
    }

    if not text:
        return result

    text = clean_text(text)

    labels = [
        ' Ref No ',
        ' UNDP Office/Country ',
        ' Process ',
        ' Deadline ',
        ' Posted ',
    ]

    positions = {}
    lower_text = text.lower()

    for label in labels:
        idx = lower_text.find(label.lower())
        positions[label.strip()] = idx

    # Title = from start until Ref No if present, else full text
    ref_idx = positions.get('Ref No', -1)
    if ref_idx != -1:
        result['title'] = clean_text(text[:ref_idx])
    else:
        result['title'] = text
        return result

    office_idx = positions.get('UNDP Office/Country', -1)
    process_idx = positions.get('Process', -1)
    deadline_idx = positions.get('Deadline', -1)
    posted_idx = positions.get('Posted', -1)

    # Ref No
    if ref_idx != -1:
        ref_start = ref_idx + len(' Ref No ')
        ref_end_candidates = [i for i in [office_idx, process_idx, deadline_idx, posted_idx] if i != -1 and i > ref_idx]
        ref_end = min(ref_end_candidates) if ref_end_candidates else len(text)
        result['reference_no'] = clean_text(text[ref_start:ref_end])

    # Office/Country
    if office_idx != -1:
        office_start = office_idx + len(' UNDP Office/Country ')
        office_end_candidates = [i for i in [process_idx, deadline_idx, posted_idx] if i != -1 and i > office_idx]
        office_end = min(office_end_candidates) if office_end_candidates else len(text)
        result['office_country_raw'] = clean_text(text[office_start:office_end])

    # Process
    if process_idx != -1:
        process_start = process_idx + len(' Process ')
        process_end_candidates = [i for i in [deadline_idx, posted_idx] if i != -1 and i > process_idx]
        process_end = min(process_end_candidates) if process_end_candidates else len(text)
        result['procurement_process'] = clean_text(text[process_start:process_end])

    # Deadline
    if deadline_idx != -1:
        deadline_start = deadline_idx + len(' Deadline ')
        deadline_end = posted_idx if posted_idx != -1 and posted_idx > deadline_idx else len(text)
        result['deadline_text'] = clean_text(text[deadline_start:deadline_end])

    # Posted
    if posted_idx != -1:
        posted_start = posted_idx + len(' Posted ')
        result['posted_text'] = clean_text(text[posted_start:])

    office, country = split_office_country(result['office_country_raw'])
    result['office'] = office
    result['country'] = country
    result['deadline'] = parse_undp_datetime(result['deadline_text'])
    result['posted_date'] = parse_undp_datetime(result['posted_text'])

    # Final cleanup in case title still accidentally includes labels
    for marker in [' Ref No ', ' UNDP Office/Country ', ' Process ', ' Deadline ', ' Posted ']:
        marker_idx = result['title'].lower().find(marker.lower().strip())
        if marker_idx != -1:
            result['title'] = clean_text(result['title'][:marker_idx])

    return result


def notify_match(match):
    recipients = set()

    for watcher in match.source.watchers.filter(is_active=True, user__is_active=True):
        if watcher.notify_in_app:
            recipients.add(watcher.user)

    if not recipients:
        for user in get_admin_recipients():
            recipients.add(user)

    deadline_note = "Not provided"
    if match.deadline:
        if match.is_deadline_passed:
            deadline_note = f"Passed on {match.deadline:%d-%b-%Y %H:%M}"
        elif match.is_deadline_soon:
            deadline_note = f"Closing soon: {match.deadline:%d-%b-%Y %H:%M}"
        else:
            deadline_note = f"{match.deadline:%d-%b-%Y %H:%M}"

    for recipient in recipients:
        CoreNotification.objects.create(
            recipient=recipient,
            sender=recipient,
            notification_type='task',
            title=f'New opportunity: {match.title[:90]}',
            message=(
                f'Ref: {match.reference_no or "N/A"} | '
                f'Country: {match.country or "N/A"} | '
                f'Office: {match.office or "N/A"} | '
                f'Process: {match.procurement_process or "N/A"} | '
                f'Deadline: {deadline_note}'
            ),
            link=f'/opportunities/matches/{match.id}/',
        )


def create_match_if_new(
    source,
    keyword,
    title,
    summary,
    link,
    published_at=None,
    deadline=None,
    reference_no='',
    country='',
    office='',
    procurement_process='',
    posted_date=None,
):
    if not title or not link:
        return None

    normalized_ref = normalize_reference(reference_no)

    if normalized_ref and OpportunityMatch.objects.filter(
        source=source,
        reference_no__iexact=normalized_ref
    ).exists():
        return None

    fingerprint = make_fingerprint(source.id, title, link, normalized_ref)

    if OpportunityMatch.objects.filter(fingerprint=fingerprint).exists():
        return None

    match = OpportunityMatch.objects.create(
        source=source,
        keyword=keyword,
        title=clean_text(title)[:300],
        summary=clean_text(summary)[:3000],
        external_url=link,
        published_at=published_at,
        deadline=deadline,
        reference_no=normalized_ref,
        country=clean_text(country)[:150],
        office=clean_text(office)[:200],
        procurement_process=clean_text(procurement_process)[:200],
        posted_date=posted_date,
        fingerprint=fingerprint,
    )
    notify_match(match)
    return match


def get_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 OpportunityMonitor/1.0"
    })
    return session


def extract_labeled_value_from_soup(soup, labels):
    label_set = {label.lower() for label in labels}

    for dt in soup.find_all('dt'):
        label = clean_text(dt.get_text(" ", strip=True)).lower()
        if label in label_set:
            dd = dt.find_next('dd')
            if dd:
                return clean_text(dd.get_text(" ", strip=True))

    for row in soup.find_all(['tr', 'div', 'li', 'p']):
        text = clean_text(row.get_text(" ", strip=True))
        if not text:
            continue
        for label in labels:
            if text.lower().startswith(label.lower()):
                parts = re.split(
                    rf'^{re.escape(label)}\s*[:\-]?\s*',
                    text,
                    maxsplit=1,
                    flags=re.IGNORECASE
                )
                if len(parts) == 2:
                    return clean_text(parts[1])

    page_text = soup.get_text("\n", strip=True)
    for label in labels:
        pattern = rf'{re.escape(label)}\s*[:\-]?\s*(.+)'
        match = re.search(pattern, page_text, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(1).split('\n')[0])

    return ''


def fetch_detail_fields(session, detail_url):
    result = {
        'title': '',
        'summary': '',
        'reference_no': '',
        'office_country_raw': '',
        'office': '',
        'country': '',
        'procurement_process': '',
        'posted_date': None,
        'deadline': None,
        'published_at': None,
    }

    if not detail_url:
        return result

    try:
        response = session.get(detail_url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        # Try heading first
        title_node = soup.find(['h1', 'h2'])
        if title_node:
            raw_title = clean_text(title_node.get_text(" ", strip=True))
            parsed = parse_compound_procurement_text(raw_title)
            result['title'] = parsed['title'] or raw_title
            result['reference_no'] = parsed['reference_no']
            result['office_country_raw'] = parsed['office_country_raw']
            result['office'] = parsed['office']
            result['country'] = parsed['country']
            result['procurement_process'] = parsed['procurement_process']
            result['deadline'] = parsed['deadline']
            result['posted_date'] = parsed['posted_date']
            result['published_at'] = parsed['posted_date']

        paragraphs = soup.find_all(['p', 'div'])
        summary_bits = []
        for p in paragraphs[:20]:
            text = clean_text(p.get_text(" ", strip=True))
            if len(text) > 60:
                summary_bits.append(text)
            if len(" ".join(summary_bits)) > 1200:
                break
        result['summary'] = " ".join(summary_bits)[:2000]

        # NEW:
        # If the summary/body contains the flattened procurement text, parse it too.
        full_page_text = clean_text(soup.get_text(" ", strip=True))
        if (
            'Ref No' in full_page_text
            and 'UNDP Office/Country' in full_page_text
            and 'Process' in full_page_text
            and 'Deadline' in full_page_text
            and 'Posted' in full_page_text
        ):
            parsed_page = parse_compound_procurement_text(full_page_text)

            if parsed_page.get('title') and not result['title']:
                result['title'] = parsed_page['title']
            if parsed_page.get('reference_no') and not result['reference_no']:
                result['reference_no'] = parsed_page['reference_no']
            if parsed_page.get('office_country_raw') and not result['office_country_raw']:
                result['office_country_raw'] = parsed_page['office_country_raw']
            if parsed_page.get('office') and not result['office']:
                result['office'] = parsed_page['office']
            if parsed_page.get('country') and not result['country']:
                result['country'] = parsed_page['country']
            if parsed_page.get('procurement_process') and not result['procurement_process']:
                result['procurement_process'] = parsed_page['procurement_process']
            if parsed_page.get('posted_date') and not result['posted_date']:
                result['posted_date'] = parsed_page['posted_date']
                result['published_at'] = parsed_page['posted_date']
            if parsed_page.get('deadline') and not result['deadline']:
                result['deadline'] = parsed_page['deadline']

        ref_value = extract_labeled_value_from_soup(
            soup,
            ['Reference Number', 'Reference No', 'Ref No', 'Ref. No', 'Solicitation Number']
        )
        office_country_value = extract_labeled_value_from_soup(
            soup,
            ['UNDP Office/Country', 'Office/Country', 'UNDP Office', 'Office']
        )
        process_value = extract_labeled_value_from_soup(
            soup,
            ['Procurement Process', 'Process']
        )
        posted_text = extract_labeled_value_from_soup(
            soup,
            ['Posted On', 'Date Posted', 'Posted', 'Publication Date']
        )
        deadline_text = extract_labeled_value_from_soup(
            soup,
            ['Deadline', 'Submission Deadline', 'Closing Date']
        )

        if ref_value and not result['reference_no']:
            result['reference_no'] = ref_value

        if office_country_value and not result['office_country_raw']:
            result['office_country_raw'] = office_country_value
            office, country = split_office_country(office_country_value)
            result['office'] = office
            result['country'] = country

        if process_value and not result['procurement_process']:
            result['procurement_process'] = process_value

        if posted_text and not result['posted_date']:
            result['posted_date'] = parse_undp_datetime(posted_text)
            result['published_at'] = result['posted_date']

        if deadline_text and not result['deadline']:
            result['deadline'] = parse_undp_datetime(deadline_text)

        return result

    except Exception:
        logger.exception("Failed to fetch detail page %s", detail_url)
        return result


def scan_undp_table_source(source):
    created = 0
    session = get_session()

    response = session.get(source.url, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, 'html.parser')
    rows = soup.find_all('tr')

    for row in rows:
        cells = row.find_all(['td', 'th'])
        if len(cells) < 6:
            continue

        header_joined = " | ".join(clean_text(cell.get_text(" ", strip=True)).lower() for cell in cells)
        if 'title' in header_joined and 'ref no' in header_joined and 'deadline' in header_joined:
            continue

        title_text = clean_text(cells[0].get_text(" ", strip=True))
        ref_text = clean_text(cells[1].get_text(" ", strip=True))
        office_country_text = clean_text(cells[2].get_text(" ", strip=True))
        process_text = clean_text(cells[3].get_text(" ", strip=True))
        deadline_text = clean_text(cells[4].get_text(" ", strip=True))
        posted_text = clean_text(cells[5].get_text(" ", strip=True))

        if not title_text or not ref_text:
            continue

        office, country = split_office_country(office_country_text)
        deadline = parse_undp_datetime(deadline_text)
        posted_date = parse_undp_datetime(posted_text)

        link_tag = cells[0].find('a', href=True)

        # IMPORTANT:
        # only fetch detail page if the row really has its own detail link
        detail = {}
        detail_url = source.url
        if link_tag and link_tag.get('href'):
            detail_url = urljoin(source.url, link_tag['href'])
            detail = fetch_detail_fields(session, detail_url)
        else:
            detail = {
                'title': '',
                'summary': '',
                'reference_no': '',
                'office_country_raw': '',
                'office': '',
                'country': '',
                'procurement_process': '',
                'posted_date': None,
                'deadline': None,
                'published_at': None,
            }

        # Prefer row/table values first because UNDP listing is already structured
        final_title = title_text
        final_reference = normalize_reference(ref_text)
        final_office = office
        final_country = country
        final_process = process_text
        final_posted_date = posted_date
        final_deadline = deadline

        # Only use detail values to fill missing gaps, not to overwrite good table data
        if detail.get('title') and len(detail['title']) < len(final_title):
            final_title = detail['title']

        if detail.get('reference_no') and not final_reference:
            final_reference = normalize_reference(detail['reference_no'])

        if detail.get('office') and not final_office:
            final_office = detail['office']

        if detail.get('country') and not final_country:
            final_country = detail['country']

        if detail.get('procurement_process') and not final_process:
            final_process = detail['procurement_process']

        if detail.get('posted_date') and not final_posted_date:
            final_posted_date = detail['posted_date']

        if detail.get('deadline') and not final_deadline:
            final_deadline = detail['deadline']

        final_summary = detail.get('summary') or clean_text(
            f"{title_text} | Ref: {ref_text} | Office/Country: {office_country_text} | "
            f"Process: {process_text} | Deadline: {deadline_text} | Posted: {posted_text}"
        )

        combined_text = " ".join([
            final_title,
            final_reference,
            final_office,
            final_country,
            final_process,
            final_summary,
        ])

        keyword = find_matching_keyword(combined_text)

        match = create_match_if_new(
            source=source,
            keyword=keyword,
            title=final_title,
            summary=final_summary,
            link=detail_url if detail_url else source.url,
            deadline=final_deadline,
            reference_no=final_reference,
            country=final_country,
            office=final_office,
            procurement_process=final_process,
            posted_date=final_posted_date,
            published_at=final_posted_date,
        )
        if match:
            created += 1

    return created


def scan_html_source(source):
    created = 0
    session = get_session()

    response = session.get(source.url, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, 'html.parser')
    candidates = soup.find_all(['a', 'article', 'div', 'li'])

    seen = set()

    for block in candidates:
        text = " ".join(block.stripped_strings)
        text = clean_text(text)

        if not text or len(text) < 20:
            continue

        link = block.get('href') if block.name == 'a' else None
        if not link:
            anchor = block.find('a', href=True)
            link = anchor['href'] if anchor else source.url

        full_link = urljoin(source.url, link)

        # NEW:
        # If the block contains a flattened UNDP procurement row, parse it properly.
        parsed = None
        if (
            'Ref No' in text
            and 'UNDP Office/Country' in text
            and 'Process' in text
            and 'Deadline' in text
            and 'Posted' in text
        ):
            parsed = parse_compound_procurement_text(text)

        if parsed:
            final_title = clean_text(parsed.get('title', ''))
            final_reference = normalize_reference(parsed.get('reference_no', ''))
            final_office = clean_text(parsed.get('office', ''))
            final_country = clean_text(parsed.get('country', ''))
            final_process = clean_text(parsed.get('procurement_process', ''))
            final_deadline = parsed.get('deadline')
            final_posted_date = parsed.get('posted_date')
            final_summary = text[:1000]

            combined_text = " ".join([
                final_title,
                final_reference,
                final_office,
                final_country,
                final_process,
                final_summary,
            ])

            keyword = find_matching_keyword(combined_text)
            if not keyword:
                keyword = find_matching_keyword(final_title)

            if not final_title:
                continue

            dedupe_key = (final_title, full_link, final_reference)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            match = create_match_if_new(
                source=source,
                keyword=keyword,
                title=final_title,
                summary=final_summary,
                link=full_link,
                published_at=final_posted_date,
                deadline=final_deadline,
                reference_no=final_reference,
                country=final_country,
                office=final_office,
                procurement_process=final_process,
                posted_date=final_posted_date,
            )
            if match:
                created += 1
            continue

        # Existing generic HTML logic
        keyword = find_matching_keyword(text)
        if not keyword:
            continue

        title = clean_text(text[:300])

        if (title, full_link) in seen:
            continue
        seen.add((title, full_link))

        match = create_match_if_new(
            source=source,
            keyword=keyword,
            title=title,
            summary=text[:1000],
            link=full_link,
        )
        if match:
            created += 1

    return created


def scan_rss_source(source):
    created = 0
    feed = feedparser.parse(source.url)

    for entry in feed.entries:
        title = getattr(entry, 'title', '') or ''
        summary = getattr(entry, 'summary', '') or getattr(entry, 'description', '') or ''
        link = getattr(entry, 'link', '') or source.url

        text = f"{title}\n{summary}"
        keyword = find_matching_keyword(text)
        if not keyword:
            continue

        published_at = None
        parsed_date = getattr(entry, 'published_parsed', None)
        if parsed_date:
            published_at = timezone.datetime(
                parsed_date.tm_year,
                parsed_date.tm_mon,
                parsed_date.tm_mday,
                parsed_date.tm_hour,
                parsed_date.tm_min,
                parsed_date.tm_sec,
                tzinfo=timezone.utc,
            )

        match = create_match_if_new(
            source=source,
            keyword=keyword,
            title=title[:300],
            summary=summary[:1000],
            link=link,
            published_at=published_at,
        )
        if match:
            created += 1

    return created


def scan_source(source):
    created = 0
    source.last_checked_at = timezone.now()

    try:
        if source.source_type == 'rss':
            created = scan_rss_source(source)
        elif source.source_type == 'undp_table':
            created = scan_undp_table_source(source)
        else:
            created = scan_html_source(source)

        source.last_status = 'ok'
        source.last_error = ''
        source.save(update_fields=['last_checked_at', 'last_status', 'last_error'])
        return created

    except Exception as exc:
        logger.exception("Scan failed for source %s", source.url)
        source.last_status = 'error'
        source.last_error = str(exc)
        source.save(update_fields=['last_checked_at', 'last_status', 'last_error'])
        return 0


def scan_all_sources():
    total = 0
    for source in OpportunitySource.objects.filter(is_active=True):
        total += scan_source(source)
    return total