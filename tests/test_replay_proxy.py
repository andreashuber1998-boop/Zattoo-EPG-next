import unittest
import xml.etree.ElementTree as ET
from replay_proxy import build_replay_mpd, local_name

MPD = b'''<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic" timeShiftBufferDepth="PT10800S" minimumUpdatePeriod="PT2S" availabilityStartTime="1970-01-01T00:00:00Z"><Period><AdaptationSet><Representation><SegmentTemplate timescale="1000" presentationTimeOffset="0" media="v-$Time$.m4s"><SegmentTimeline><S t="10000000" d="2000" r="5399"/></SegmentTimeline></SegmentTemplate></Representation></AdaptationSet></Period></MPD>'''

class ReplayProxyTests(unittest.TestCase):
    def test_crops_three_hour_window_to_two_hours(self):
        root = ET.fromstring(build_replay_mpd(MPD, 7200))
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

if __name__ == "__main__": unittest.main()
