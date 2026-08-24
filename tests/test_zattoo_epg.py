import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from zattoo_epg import ZattooEPG, get_credentials_from_config, load_channel_filter


class ZattooEPGTests(unittest.TestCase):
    def test_channel_filter_matches_exact_id_or_title(self):
        epg = ZattooEPG(channel_filter=["SRF 1 HD", "srf2.ch"])

        self.assertTrue(epg._channel_is_selected("SRF1.ch", "SRF 1 HD"))
        self.assertTrue(epg._channel_is_selected("SRF2.ch", "SRF zwei HD"))
        self.assertFalse(epg._channel_is_selected("SRFinfo.ch", "SRF info HD"))

    def test_detail_cache_avoids_repeated_api_lookup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, "details.sqlite3")
            epg = ZattooEPG(cache_path=cache_path)
            epg.epg_data = [
                {"id": 1, "cid": "one"},
                {"id": 1, "cid": "one"},
                {"id": 2, "cid": "two"},
            ]
            epg._store_cached_details({"1": {"i_t": "cached-image"}})

            with mock.patch.object(
                epg,
                "get_program_details_batch",
                return_value={"2": {"i_t": "new-image"}},
            ) as api_call, mock.patch("zattoo_epg.time.sleep"):
                epg.enhance_epg_data()

            api_call.assert_called_once_with(["2"])
            self.assertEqual(epg.epg_data[0]["i_t"], "cached-image")
            self.assertEqual(epg.epg_data[1]["i_t"], "cached-image")
            self.assertEqual(epg.epg_data[2]["i_t"], "new-image")

    def test_detail_cache_supports_large_guides(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            epg = ZattooEPG(cache_path=os.path.join(temp_dir, "details.sqlite3"))
            details = {str(index): {"title": f"Programme {index}"} for index in range(1005)}
            epg._store_cached_details(details)

            self.assertEqual(len(epg._load_cached_details(list(details))), 1005)

    def test_xmltv_contains_image_and_correct_winter_timezone(self):
        epg = ZattooEPG(country="CH")
        epg.channels = {"channel-1": {"title": "A & B", "logo": "https://logo"}}
        epg.epg_data = [
            {
                "id": 1,
                "cid": "channel-1",
                "s": 1767261600,
                "e": 1767265200,
                "t": "News & Weather",
                "i_t": "poster-id",
            }
        ]

        xml_data = epg.generate_xmltv(filename=None, return_data=True)
        root = ET.fromstring(xml_data)
        programme = root.find("programme")

        self.assertEqual(root.findtext("channel/display-name"), "A & B")
        self.assertEqual(programme.findtext("title"), "News & Weather")
        self.assertTrue(programme.attrib["start"].endswith("+0100"))
        self.assertEqual(
            programme.find("icon").attrib["src"],
            "https://images.zattic.com/cms/poster-id/original.jpg",
        )

    def test_environment_credentials_take_precedence(self):
        with mock.patch.dict(
            os.environ,
            {"ZATTOO_EMAIL": "user@example.com", "ZATTOO_PASSWORD": "secret"},
            clear=False,
        ):
            self.assertEqual(
                get_credentials_from_config("missing.json"),
                ("user@example.com", "secret"),
            )

    def test_channel_filter_file_ignores_comments(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write("# comment\nSRF 1 HD\n\nZDF HD\n")
            path = handle.name
        try:
            self.assertEqual(load_channel_filter(path), ["SRF 1 HD", "ZDF HD"])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
