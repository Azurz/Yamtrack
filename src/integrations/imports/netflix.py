import json
import logging
import re
import unicodedata
from collections import defaultdict
from csv import DictReader
from datetime import datetime

import requests
from django.apps import apps
from django.utils import timezone

import app
from app.models import MediaTypes, Sources, Status
from app.providers import services
from integrations.imports import helpers
from integrations.imports.helpers import MediaImportError, MediaImportUnexpectedError

logger = logging.getLogger(__name__)

# Matches episodic markers found after ":" in Netflix titles such as:
#   "Show: Season 2: ...", "Show: Saison 1: ...", "Show: Volume 3: ...",
#   "Show: Mini-série: ...", "Show: S01E02", etc.
# Used to distinguish clearly episodic TV entries from titles that merely
# contain a colon (e.g. "Mission: Impossible", "Fast & Furious: Hobbs & Shaw").
# Note: \b is omitted so that mojibake artefacts like "SaisonÂ" still match.
_EPISODIC_PATTERN = re.compile(
    r":\s*("
    r"Season|Saison|Temporada|Staffel|Stagione"
    r"|Episode|Épisode|Episodio"
    r"|Chapter|Chapitre|Cap[íi]tulo"
    r"|Partie\s+\d|Parte\s+\d|Part\s+\d"
    r"|Volume\s+\d"
    r"|Mini[- ]?s[eé]rie|Miniserie"
    r"|Pilote|Pilot"
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
        """Normalize a Netflix title to a searchable base title.

        Netflix watch history appends episode/season details after ":".
        Always stripping at the first ":" ensures that multiple rows for the
        same show (e.g. "Breaking Bad: Saison 5: Episode 1" and
        "Breaking Bad: Saison 5: Episode 2") are grouped under one entry.
        """
        title = self._fix_mojibake(raw_title.strip())

        if ":" in title:
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
        return unicodedata.normalize("NFKC", result)

    def _lookup_media(self, title, raw_title):
        """Look up title in TMDB with a multi-pass strategy.

        Search order (TV-first vs Movie-first) is determined by whether the
        raw Netflix title contains explicit episodic markers (Season, Saison,
        Volume, Mini-série, S01E02, etc.).

        For non-episodic entries that were stripped at ":" (e.g. "Nightflyers:
        Greywing" → title="Nightflyers"), the full raw_title is also tried so
        that movies with subtitles ("X-Men : Le Commencement") are found under
        their complete French title before falling back to the stripped form.
        """
        is_episodic = bool(_EPISODIC_PATTERN.search(raw_title))

        if is_episodic:
            # Clearly a TV episode: search TV first, only need the show name.
            search_order = (MediaTypes.TV.value, MediaTypes.MOVIE.value)
            titles_to_try = [title]
        else:
            # Could be a movie or a TV show without explicit season markers.
            # Try the full raw_title first (handles "Movie: Subtitle"),
            # then the stripped title (handles "Show: Episode Title").
            search_order = (MediaTypes.MOVIE.value, MediaTypes.TV.value)
            titles_to_try = [title] if raw_title == title else [raw_title, title]

        # Add sanitized variants as final fallbacks for special characters.
        for base in list(titles_to_try):
            sanitized = self._sanitize_title_for_search(base)
            if sanitized != base and sanitized not in titles_to_try:
                titles_to_try.append(sanitized)

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


def api_importer(credentials, user, mode):
    """Import watch history from Netflix API using session cookies."""
    importer = NetflixApiImporter(credentials, user, mode)
    return importer.import_data()


class NetflixApiImporter(NetflixImporter):
    """Import Netflix watch history via the unofficial internal API.

    Requires two session cookies from an active Netflix browser session:
      - NetflixId
      - SecureNetflixId

    Netflix does not expose a public API; this uses the same internal
    endpoint that powers the "Viewing Activity" page.
    """

    _BROWSE_URL = "https://www.netflix.com/browse"
    _ACTIVITY_URL = "https://www.netflix.com/api/shakti/{build_id}/viewingactivity"
    _PAGE_SIZE = 100

    def __init__(self, credentials, user, mode):
        # credentials is a dict: {"netflix_id": "...", "secure_netflix_id": "..."}
        super().__init__(None, user, mode)  # file=None, not used by API path
        self.credentials = credentials

    def _build_session(self):
        session = requests.Session()
        session.cookies.set(
            "NetflixId",
            self.credentials["netflix_id"],
            domain=".netflix.com",
        )
        session.cookies.set(
            "SecureNetflixId",
            self.credentials["secure_netflix_id"],
            domain=".netflix.com",
        )
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "X-Requested-With": "XMLHttpRequest",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
        )
        return session

    def _get_build_id(self, session):
        """Extract Netflix build identifier from the browse page."""
        resp = session.get(self._BROWSE_URL, timeout=15)
        if resp.status_code != 200:
            msg = (
                f"Netflix returned HTTP {resp.status_code}. "
                "Your session cookies may be invalid or expired."
            )
            raise MediaImportError(msg)
        match = re.search(r'"BUILD_IDENTIFIER":"([a-z0-9]+)"', resp.text)
        if not match:
            msg = (
                "Could not extract Netflix build ID from the browse page. "
                "Try refreshing your session cookies."
            )
            raise MediaImportError(msg)
        return match.group(1)

    def _fetch_all_activity(self, session, build_id):
        """Paginate through Netflix viewing activity and return all items."""
        url = self._ACTIVITY_URL.format(build_id=build_id)
        items = []
        page = 0
        while True:
            resp = session.get(
                url,
                params={
                    "pg": page,
                    "pgSize": self._PAGE_SIZE,
                    "from": page * self._PAGE_SIZE,
                },
                headers={"Referer": "https://www.netflix.com/viewingactivity"},
                timeout=15,
            )
            if resp.status_code == 421:
                msg = (
                    "Netflix rejected the request (HTTP 421). "
                    "This usually means Netflix blocked the server IP. "
                    "Try using the CSV import instead: go to netflix.com/viewingactivity "
                    "and click 'Download all'."
                )
                raise MediaImportError(msg)
            if resp.status_code != 200:
                msg = f"Netflix activity API returned HTTP {resp.status_code}."
                raise MediaImportError(msg)
            batch = resp.json().get("viewedItems", [])
            items.extend(batch)
            if len(batch) < self._PAGE_SIZE:
                break
            page += 1
        return items

    def import_data(self):
        """Fetch viewing history from Netflix API and import into Yamtrack."""
        session = self._build_session()
        build_id = self._get_build_id(session)
        all_items = self._fetch_all_activity(session, build_id)

        grouped_rows = {}
        for item in all_items:
            try:
                raw_title = (item.get("title") or "").strip()
                date_ms = item.get("date")
                watch_date = None
                if date_ms:
                    watch_date = datetime.fromtimestamp(
                        date_ms / 1000,
                        tz=timezone.get_current_timezone(),
                    ).replace(hour=0, minute=0, second=0, microsecond=0)

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
                error_msg = f"Error processing Netflix API entry: {item}"
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
