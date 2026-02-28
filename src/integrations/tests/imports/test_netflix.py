from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from app.models import TV, MediaTypes, Movie, Status
from integrations.imports import netflix

mock_path = Path(__file__).resolve().parent.parent / "mock_data"


class ImportNetflix(TestCase):
    """Test importing media from Netflix CSV."""

    def setUp(self):
        """Create user for the tests."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)

    @patch("integrations.imports.netflix.services.search")
    def test_import_netflix_csv(self, mock_search):
        """Test importing movies and TV shows from Netflix viewing history CSV."""

        def fake_search(media_type, query, page, source):  # noqa: ARG001
            if media_type == MediaTypes.MOVIE.value and query == "Inception":
                return {
                    "results": [
                        {"media_id": "27205", "title": "Inception", "image": "https://img"},
                    ],
                }
            if media_type == MediaTypes.TV.value and query == "Stranger Things":
                return {
                    "results": [
                        {
                            "media_id": "66732",
                            "title": "Stranger Things",
                            "image": "https://img2",
                        },
                    ],
                }
            return {"results": []}

        mock_search.side_effect = fake_search

        with Path(mock_path / "import_netflix.csv").open("rb") as file:
            imported_counts, warnings = netflix.importer(file, self.user, "new")

        self.assertEqual(imported_counts[MediaTypes.MOVIE.value], 1)
        self.assertEqual(imported_counts[MediaTypes.TV.value], 1)

        movie = Movie.objects.get(item__title="Inception")
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertEqual(movie.progress, 1)
        self.assertEqual(
            movie.end_date,
            datetime(2024, 1, 2, tzinfo=timezone.get_current_timezone()),
        )

        tv = TV.objects.get(item__title="Stranger Things")
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)

        self.assertIn(
            "Unknown Show: Couldn't find a match in The Movie Database",
            warnings,
        )

    def test_parse_date(self):
        """Test Netflix date parser."""
        importer = netflix.NetflixImporter(None, self.user, "new")

        parsed = importer._parse_date("2024-08-01")
        self.assertEqual(parsed.date(), datetime(2024, 8, 1, tzinfo=UTC).date())

        self.assertIsNone(importer._parse_date(""))
        self.assertIsNone(importer._parse_date("not-a-date"))
