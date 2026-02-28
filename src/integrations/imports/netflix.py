import logging
import re
import unicodedata
from collections import defaultdict
from csv import DictReader
from datetime import datetime

from django.apps import apps
from django.utils import timezone

import app
from app.models import MediaTypes, Sources, Status
from app.providers import services
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError

logger = logging.getLogger(__name__)

# Matches patterns like ": Season 1", ": Saison 2", ": Episode 3", ": S01E01", etc.
# Used to distinguish episodic TV entries from movie titles that happen to contain ":".
# Note: \b is intentionally omitted so that mojibake artefacts such as "SaisonÂ"
# (where the accented letter is a \w character) are still matched correctly.
_EPISODIC_PATTERN = re.compile(
    r":\s*("
    r"Season|Saison|Temporada|Staffel|Stagione"
    r"|Episode|Épisode|Episodio"
    r"|Chapter|Chapitre|Cap[íi]tulo"
    r"|Partie\s+\d|Parte\s+\d|Part\s+\d"
    r"|S\d{1,2}(?:E\d{1,2})?"
    r")",
    re.IGNORECASE,
)


def importer(file, user, mode):
    """Import watch history from a Netflix CSV export."""
    netflix_importer = NetflixImporter(file, user, mode)
    return netflix_importer.import_data()


class NetflixImporter:
    """Class to handle importing Netflix watch history from CSV."""

    def __init__(self, file, user, mode):
        self.file = file
        self.user = user
        self.mode = mode
        self.warnings = []

        self.existing_media = helpers.get_existing_media(user)
        self.to_delete = defaultdict(lambda: defaultdict(set))
        self.bulk_media = defaultdict(list)

        logger.info(
            "Initialized Netflix CSV importer for user %s with mode %s",
            user.username,
            mode,
        )

    def import_data(self):
        """Import all Netflix rows from the CSV file."""
        decoded_file = self._decode_file_content()
        reader = DictReader(decoded_file.splitlines())

        # Keep one entry per normalized title, storing latest watch date.
        grouped_rows = {}
        for row in reader:
            try:
                raw_title = (row.get("Title") or "").strip()
                # Support both old ("Date") and new ("Start Time") Netflix CSV formats.
                raw_date = row.get("Date") or row.get("Start Time", "")
                watch_date = self._parse_netflix_date(raw_date)
                base_title = self._normalize_title(raw_title)

                if not base_title:
                    continue

                key = base_title.casefold()
                existing = grouped_rows.get(key)
                if not existing or (
                    watch_date is not None
                    and (
                        existing["watch_date"] is None
                        or watch_date > existing["watch_date"]
                    )
                ):
                    grouped_rows[key] = {
                        "title": base_title,
                        "watch_date": watch_date,
                        "raw_title": raw_title,
                    }
            except Exception as error:
                error_msg = f"Error processing entry: {row}"
                raise MediaImportUnexpectedError(error_msg) from error

        for grouped in grouped_rows.values():
            try:
                self._import_grouped_row(grouped)
            except Exception as error:
                error_msg = f"Error importing grouped entry: {grouped}"
                raise MediaImportUnexpectedError(error_msg) from error

        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)

        imported_counts = {
            media_type: len(media_list)
            for media_type, media_list in self.bulk_media.items()
        }
        deduplicated_messages = "\n".join(dict.fromkeys(self.warnings))
        return imported_counts, deduplicated_messages if self.warnings else None

    def _decode_file_content(self):
        """Decode uploaded bytes with robust fallback for common CSV encodings."""
        raw = self.file.read()
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        msg = "Invalid file format. Please upload a valid Netflix CSV file."
        raise MediaImportError(msg)

    def _fix_mojibake(self, text):
        """Best-effort fix for common UTF-8/latin1 mojibake artifacts."""
        if not text or not any(marker in text for marker in ("Ã", "Â")):
            return text

        try:
            return text.encode("latin1").decode("utf-8")
        except UnicodeError:
            return text

    def _normalize_title(self, raw_title):
        """Normalize Netflix title rows to a searchable top-level title."""
        title = self._fix_mojibake(raw_title.strip())

        # Only truncate on ":" when genuine episodic markers are present
        # (e.g. "Season 2", "Saison 1", "Episode 3").  This preserves movie
        # titles that legitimately contain a colon such as
        # "Fast & Furious Presents: Hobbs & Shaw".
        if _EPISODIC_PATTERN.search(title):
            title = title.split(":", 1)[0].strip()

        return title

    def _parse_netflix_date(self, value):
        """Parse Netflix date formats.

        Handles the old format ("Date" column: M/D/YY or YYYY-MM-DD) and the
        newer export format ("Start Time" column: YYYY/MM/DD HH:MM:SS).
        """
        date_str = (value or "").strip()
        if not date_str:
            return None

        for fmt in (
            "%m/%d/%y",
            "%m/%d/%Y",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                parsed = datetime.strptime(date_str, fmt)
                return parsed.replace(
                    hour=0,
                    minute=0,
                    second=0,
                    microsecond=0,
                    tzinfo=timezone.get_current_timezone(),
                )
            except ValueError:
                continue

        logger.warning("Could not parse Netflix date: %s", date_str)
        return None

    def _sanitize_title_for_search(self, title):
        """Return a fallback title with special characters simplified.

        Replaces characters like the masculine ordinal indicator (º → o) that
        TMDB may not index, so a secondary search attempt can succeed.
        """
        replacements = {
            "\u00ba": "o",  # º masculine ordinal indicator
            "\u00aa": "a",  # ª feminine ordinal indicator
            "\u2116": "No",  # № numero sign
            "\u00b0": "",  # ° degree sign
        }
        result = title
        for char, replacement in replacements.items():
            result = result.replace(char, replacement)
        # Also apply NFKC normalization to resolve other compatibility chars
        return unicodedata.normalize("NFKC", result)

    def _lookup_media(self, title, raw_title):
        """Look up title in TMDB with strategy based on Netflix row shape."""
        # Only treat an entry as episodic when actual season/episode markers
        # are present.  A bare ":" (e.g. "Hobbs & Shaw") is not enough.
        if _EPISODIC_PATTERN.search(raw_title):
            search_order = (MediaTypes.TV.value, MediaTypes.MOVIE.value)
        else:
            search_order = (MediaTypes.MOVIE.value, MediaTypes.TV.value)

        # Try the normalized title first; fall back to a sanitized version if needed.
        sanitized = self._sanitize_title_for_search(title)
        titles_to_try = [title] if sanitized == title else [title, sanitized]

        for search_title in titles_to_try:
            for media_type in search_order:
                results = services.search(
                    media_type,
                    search_title,
                    1,
                    Sources.TMDB.value,
                ).get("results", [])
                if results:
                    first = results[0]
                    return {
                        "title": first["title"],
                        "image": first["image"],
                        "media_id": str(first["media_id"]),
                        "media_type": media_type,
                    }
        return None

    def _import_grouped_row(self, grouped):
        title = grouped["title"]
        watch_date = grouped["watch_date"]
        match = self._lookup_media(title, grouped["raw_title"])

        if not match:
            self.warnings.append(
                f"{title}: Couldn't find a match in {Sources.TMDB.label}",
            )
            return

        media_type = match["media_type"]

        if not helpers.should_process_media(
            self.existing_media,
            self.to_delete,
            media_type,
            Sources.TMDB.value,
            match["media_id"],
            self.mode,
        ):
            return

        item, _ = app.models.Item.objects.update_or_create(
            media_id=match["media_id"],
            source=Sources.TMDB.value,
            media_type=media_type,
            defaults={
                "title": match["title"],
                "image": match["image"],
            },
        )

        model = apps.get_model(app_label="app", model_name=media_type)
        if media_type == MediaTypes.MOVIE.value:
            instance = model(
                item=item,
                user=self.user,
                status=Status.COMPLETED.value,
                progress=1,
                end_date=watch_date,
            )
        else:
            instance = model(
                item=item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
            )

        instance._history_date = watch_date or timezone.now()
        self.bulk_media[media_type].append(instance)
