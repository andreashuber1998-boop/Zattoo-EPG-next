import unittest
import xml.etree.ElementTree as ET
import os
import tempfile
from replay_proxy import build_catchup_mpd, build_replay_mpd, build_timeshift_mpd, load_channel_allowlist, local_name

MPD = b'''<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic" timeShiftBufferDepth="PT10800S" minimumUpdatePeriod="PT2S" availabilityStartTime="1970-01-01T00:00:00Z"><Period><AdaptationSet><Representation><SegmentTemplate timescale="1000" presentationTimeOffset="0" media="v-$Time$.m4s"><SegmentTimeline><S t="10000000" d="2000" r="5399"/></SegmentTimeline></SegmentTemplate></Representation></AdaptationSet></Period></MPD>'''

class ReplayProxyTests(unittest.TestCase):
    def test_crops_three_hour_window_to_two_hours(self):
        output = build_replay_mpd(MPD, 7200)
        self.assertIn(b'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"', output)
        self.assertNotIn(b"ns0:MPD", output)
        root = ET.fromstring(output)
        self.assertEqual(root.attrib["type"], "static")
        self.assertEqual(root.attrib["mediaPresentationDuration"], "PT7200S")
        self.assertNotIn("timeShiftBufferDepth", root.attrib)
        template = next(node for node in root.iter() if local_name(node.tag) == "SegmentTemplate")
        segment = next(node for node in root.iter() if local_name(node.tag) == "S")
        self.assertEqual(template.attrib["presentationTimeOffset"], "13600000")
        self.assertEqual(segment.attrib["t"], "13600000")
        self.assertEqual(segment.attrib["r"], "3599")

    def test_rejects_offset_outside_window(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 10800"):
            build_replay_mpd(MPD, 10801)

    def test_dynamic_timeshift_starts_near_live_and_keeps_two_hour_window(self):
        output = build_timeshift_mpd(MPD, window_seconds=7200, delay_seconds=10)
        self.assertNotIn(b"ns0:MPD", output)
        root = ET.fromstring(output)
        self.assertEqual(root.attrib["type"], "dynamic")
        self.assertEqual(root.attrib["timeShiftBufferDepth"], "PT7200S")
        self.assertEqual(root.attrib["suggestedPresentationDelay"], "PT10S")

    def test_dynamic_catchup_window_keeps_refreshing_at_live_edge(self):
        output = build_catchup_mpd(MPD, window_seconds=7200, delay_seconds=10)
        root = ET.fromstring(output)
        self.assertEqual(root.attrib["type"], "dynamic")
        self.assertEqual(root.attrib["timeShiftBufferDepth"], "PT7200S")
        self.assertEqual(root.attrib["suggestedPresentationDelay"], "PT10S")
        self.assertNotIn("mediaPresentationDuration", root.attrib)
        segment = next(node for node in root.iter() if local_name(node.tag) == "S")
        self.assertEqual(segment.attrib["t"], "13600000")
        self.assertEqual(segment.attrib["r"], "3599")

    def test_replay_channel_allowlist_ignores_comments(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write("# enabled replay channels\nprosieben\nzdf # family\n")
            path = handle.name
        try:
            self.assertEqual(load_channel_allowlist(path), {"prosieben", "zdf"})
        finally:
            os.unlink(path)

if __name__ == "__main__": unittest.main()
