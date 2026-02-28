from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from app.models import MediaTypes, Movie, Status, TV
from integrations.imports import netflix


class ImportNetflix(TestCase):
    """Test importing media from Netflix CSV."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="test",
            password="12345",
        )

    @patch("integrations.imports.netflix.services.search")
    def test_import_netflix_csv_with_us_short_dates(self, mock_search):
        """Netflix dates like M/D/YY should parse and be imported."""

        def search_side_effect(media_type, query, _limit, _source):
            if media_type == MediaTypes.TV.value and query == "Breaking Bad":
                return {
                    "results": [
                        {
                            "title": "Breaking Bad",
                            "image": "https://img/bb.jpg",
                            "media_id": "1396",
                        },
                    ],
                }
            if media_type == MediaTypes.MOVIE.value and query == "Submersion":
                return {
                    "results": [
                        {
                            "title": "Submersion",
                            "image": "https://img/sub.jpg",
                            "media_id": "123",
                        },
                    ],
                }
            return {"results": []}

        mock_search.side_effect = search_side_effect

        content = (
            "Title,Date\n"
            '"Breaking Bad: SaisonÂ 5: Nouveaux labos","2/26/26"\n'
            '"Breaking Bad: SaisonÂ 5: Madrigal","2/20/26"\n'
            '"Submersion","12/31/25"\n'
        ).encode("utf-8")

        imported_counts, warnings = netflix.importer(
            BytesIO(content),
            self.user,
            "new",
        )

        self.assertEqual(imported_counts[MediaTypes.TV.value], 1)
        self.assertEqual(imported_counts[MediaTypes.MOVIE.value], 1)
        self.assertIsNone(warnings)

        tv = TV.objects.get(item__title="Breaking Bad")
        self.assertEqual(tv.status, Status.IN_PROGRESS.value)

        movie = Movie.objects.get(item__title="Submersion")
        self.assertEqual(movie.status, Status.COMPLETED.value)
        self.assertEqual(movie.progress, 1)

    def test_parse_netflix_date(self):
        importer_instance = netflix.NetflixImporter(BytesIO(b""), self.user, "new")
        self.assertIsNotNone(importer_instance._parse_netflix_date("11/11/24"))
        self.assertIsNotNone(importer_instance._parse_netflix_date("1/1/24"))
        self.assertIsNone(importer_instance._parse_netflix_date("invalid"))

    def test_normalize_title_fixes_mojibake_and_episode_suffix(self):
        importer_instance = netflix.NetflixImporter(BytesIO(b""), self.user, "new")
        normalized = importer_instance._normalize_title(
            "Breaking Bad: SaisonÂ 4: FrÃ¨res et partenaires",
        )
        self.assertEqual(normalized, "Breaking Bad")
