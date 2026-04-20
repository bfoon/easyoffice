from django.core.management.base import BaseCommand
from apps.opportunities.models import OpportunityMatch
from apps.opportunities.services import parse_compound_procurement_text, clean_text


class Command(BaseCommand):
    help = "Repair imported opportunity matches where title contains combined procurement text."

    def handle(self, *args, **options):
        repaired = 0

        qs = OpportunityMatch.objects.all()

        for match in qs:
            title_text = clean_text(match.title or '')

            if 'Ref No ' not in title_text and 'UNDP Office/Country ' not in title_text:
                continue

            parsed = parse_compound_procurement_text(title_text)

            changed = False

            if parsed['title'] and parsed['title'] != match.title:
                match.title = parsed['title']
                changed = True

            if parsed['reference_no'] and not match.reference_no:
                match.reference_no = parsed['reference_no']
                changed = True

            if parsed['office'] and not match.office:
                match.office = parsed['office']
                changed = True

            if parsed['country'] and not match.country:
                match.country = parsed['country']
                changed = True

            if parsed['procurement_process'] and not match.procurement_process:
                match.procurement_process = parsed['procurement_process']
                changed = True

            if parsed['deadline'] and not match.deadline:
                match.deadline = parsed['deadline']
                changed = True

            if parsed['posted_date'] and not match.posted_date:
                match.posted_date = parsed['posted_date']
                if not match.published_at:
                    match.published_at = parsed['posted_date']
                changed = True

            if changed:
                match.save()
                repaired += 1

        self.stdout.write(self.style.SUCCESS(f"Repaired {repaired} opportunity matches."))