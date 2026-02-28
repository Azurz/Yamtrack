import logging
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
TV_TITLE_PARTS_THRESHOLD = 3


def importer(file, user, mode):
    """Import media from a Netflix viewing history CSV file."""
    netflix_importer = NetflixImporter(file, user, mode)
    return netflix_importer.import_data()


class NetflixImporter:
    """Class to handle importing user data from Netflix CSV."""

    def __init__(self, file, user, mode):
        """Initialize importer with uploaded file, user, and import mode."""
        self.file = file
        self.user = user
        self.mode = mode
        self.warnings = []

        self.existing_media = helpers.get_existing_media(user)
        self.to_delete = defaultdict(lambda: defaultdict(set))
        self.bulk_media = defaultdict(list)
        self.queued_media_ids = set()

    def import_data(self):
        """Import all Netflix data from the CSV file."""
        try:
            decoded_file = self.file.read().decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            msg = "Invalid file format. Please upload a CSV file."
            raise MediaImportError(msg) from error

        reader = DictReader(decoded_file)

        for row in reader:
            try:
                self._process_row(row)
            except Exception as error:
                error_msg = f"Error processing entry: {row}"
                raise MediaImportUnexpectedError(error_msg) from error

        helpers.cleanup_existing_media(self.to_delete, self.user)
        helpers.bulk_create_media(self.bulk_media, self.user)

        imported_counts = {
            media_type: len(media_list)
            for media_type, media_list in self.bulk_media.items()
        }

        deduplicated_messages = "\n".join(dict.fromkeys(self.warnings))
        return imported_counts, deduplicated_messages if self.warnings else None

    def _process_row(self, row):
        raw_title = (row.get("Title") or "").strip()
        watched_date = self._parse_date(row.get("Date", ""))

        if not raw_title:
            return

        if ":" in raw_title and len(raw_title.split(":")) >= TV_TITLE_PARTS_THRESHOLD:
            self._process_tv_row(raw_title, watched_date)
            return

        self._process_movie_or_tv_row(raw_title, watched_date)

    def _process_tv_row(self, raw_title, watched_date):
        show_title = raw_title.split(":", 1)[0].strip()

        tv_match = self._search_tmdb(MediaTypes.TV.value, show_title)

        if not tv_match:
            self.warnings.append(
                f"{raw_title}: Couldn't find a TV match in {Sources.TMDB.label}",
            )
            return

        self._queue_media(
            media_type=MediaTypes.TV.value,
            tmdb_data=tv_match,
            status=Status.IN_PROGRESS.value,
            watched_date=watched_date,
        )

    def _process_movie_or_tv_row(self, title, watched_date):
        movie_match = self._search_tmdb(MediaTypes.MOVIE.value, title)
        if movie_match:
            self._queue_media(
                media_type=MediaTypes.MOVIE.value,
                tmdb_data=movie_match,
                status=Status.COMPLETED.value,
                watched_date=watched_date,
            )
            return

        tv_match = self._search_tmdb(MediaTypes.TV.value, title)
        if tv_match:
            self._queue_media(
                media_type=MediaTypes.TV.value,
                tmdb_data=tv_match,
                status=Status.IN_PROGRESS.value,
                watched_date=watched_date,
            )
            return

        self.warnings.append(f"{title}: Couldn't find a match in {Sources.TMDB.label}")

    def _queue_media(self, media_type, tmdb_data, status, watched_date):
        media_id = str(tmdb_data["media_id"])
        source = Sources.TMDB.value
        key = (media_type, source, media_id)

        if key in self.queued_media_ids:
            return

        item, _ = app.models.Item.objects.update_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            defaults={
                "title": tmdb_data["title"],
                "image": tmdb_data.get("image", ""),
            },
        )

        if not helpers.should_process_media(
            self.existing_media,
            self.to_delete,
            media_type,
            source,
            media_id,
            self.mode,
        ):
            return

        model = apps.get_model(app_label="app", model_name=media_type)
        params = {
            "item": item,
            "user": self.user,
            "status": status,
        }

        if media_type == MediaTypes.MOVIE.value:
            params["progress"] = 1
            params["end_date"] = watched_date

        instance = model(**params)
        instance._history_date = watched_date or timezone.now()

        self.bulk_media[media_type].append(instance)
        self.queued_media_ids.add(key)

    def _search_tmdb(self, media_type, query):
        results = services.search(
            media_type=media_type,
            query=query,
            page=1,
            source=Sources.TMDB.value,
        ).get("results", [])

        if not results:
            return None

        return results[0]

    def _parse_date(self, date_str):
        date_str = (date_str or "").strip()
        if not date_str:
            return None

        date_formats = [
            "%Y-%m-%d",
            "%Y-%m-%d %H:%M:%S",
            "%d/%m/%Y",
            "%m/%d/%Y",
        ]

        for date_format in date_formats:
            try:
                return datetime.strptime(date_str, date_format).replace(
                    hour=0,
                    minute=0,
                    second=0,
                    tzinfo=timezone.get_current_timezone(),
                )
            except ValueError:
                continue

        logger.warning("Could not parse Netflix date: %s", date_str)
        return None
