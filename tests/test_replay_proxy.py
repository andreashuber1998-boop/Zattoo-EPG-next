import unittest
import xml.etree.ElementTree as ET
import os
import tempfile
from replay_proxy import CatchupTimelineState, DashHlsSession, build_catchup_mpd, build_replay_mpd, build_timeshift_mpd, load_channel_allowlist, local_name

MPD = b'''<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic" timeShiftBufferDepth="PT10800S" minimumUpdatePeriod="PT2S" availabilityStartTime="1970-01-01T00:00:00Z"><Period><AdaptationSet><Representation><SegmentTemplate timescale="1000" presentationTimeOffset="0" media="v-$Time$.m4s"><SegmentTimeline><S t="10000000" d="2000" r="5399"/></SegmentTimeline></SegmentTemplate></Representation></AdaptationSet></Period></MPD>'''

HLS_MPD = b'''<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic" timeShiftBufferDepth="PT10800S"><Period>
<AdaptationSet contentType="video" mimeType="video/mp4"><Representation id="v1" bandwidth="5000000" codecs="avc1.4d402a" width="1920" height="1080" frameRate="50"><SegmentTemplate timescale="1000" initialization="video-$RepresentationID$-$Bandwidth$-init.mp4" media="video-$RepresentationID$-$Bandwidth$-$Time$.m4s"><SegmentTimeline><S t="10000000" d="2000" r="5399"/></SegmentTimeline></SegmentTemplate></Representation></AdaptationSet>
<AdaptationSet contentType="audio" mimeType="audio/mp4"><Representation id="a1" bandwidth="256000" codecs="ec-3"><SegmentTemplate timescale="1000" initialization="audio-$RepresentationID$-init.mp4" media="audio-$RepresentationID$-$Time$.m4s"><SegmentTimeline><S t="10000000" d="2000" r="5399"/></SegmentTimeline></SegmentTemplate></Representation></AdaptationSet>
</Period></MPD>'''

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
        self.assertEqual(root.attrib["minimumUpdatePeriod"], "PT2S")
        self.assertTrue(root.attrib["publishTime"].endswith("Z"))
        segment = next(node for node in root.iter() if local_name(node.tag) == "S")
        self.assertEqual(segment.attrib["t"], "10000000")
        self.assertEqual(segment.attrib["r"], "5399")

    def test_catchup_timeline_only_appends_and_never_moves_backwards(self):
        state = CatchupTimelineState()
        first = build_catchup_mpd(
            MPD, window_seconds=7200, delay_seconds=10, state=state
        )
        shifted = MPD.replace(
            b't="10000000" d="2000" r="5399"',
            b't="10002000" d="2000" r="5399"',
        )
        second = build_catchup_mpd(
            shifted, window_seconds=7200, delay_seconds=10, state=state
        )
        first_segment = next(
            node for node in ET.fromstring(first).iter()
            if local_name(node.tag) == "S"
        )
        second_segment = next(
            node for node in ET.fromstring(second).iter()
            if local_name(node.tag) == "S"
        )
        self.assertEqual(first_segment.attrib["t"], "10000000")
        self.assertEqual(second_segment.attrib["t"], "10000000")
        self.assertEqual(first_segment.attrib["r"], "5399")
        self.assertEqual(second_segment.attrib["r"], "5400")

    def test_replay_channel_allowlist_ignores_comments(self):
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write("# enabled replay channels\nchannel-one\nchannel-two # family\n")
            path = handle.name
        try:
            self.assertEqual(load_channel_allowlist(path), {"channel-one", "channel-two"})
        finally:
            os.unlink(path)

    def test_dash_hls_gateway_builds_seekable_video_and_audio(self):
        session = DashHlsSession(
            "example-channel", window_seconds=7200, delay_seconds=10
        )
        session.update(HLS_MPD, "https://media.example/live/manifest.mpd")
        master = session.master_playlist().decode()
        video = session.media_playlist(0).decode()
        audio = session.media_playlist(1).decode()
        self.assertIn('AUDIO="audio"', master)
        self.assertIn('URI="track-1.m3u8"', master)
        self.assertIn("#EXT-X-START:TIME-OFFSET=-10", video)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:6800", video)
        self.assertEqual(video.count("#EXTINF:"), 3600)
        self.assertEqual(audio.count("#EXTINF:"), 3600)
        self.assertEqual(
            session.segment_url(0, 10000000),
            "https://media.example/live/video-v1-5000000-10000000.m4s",
        )
        self.assertEqual(
            session.segment_url(1),
            "https://media.example/live/audio-a1-init.mp4",
        )

        shifted = HLS_MPD.replace(
            b't="10000000" d="2000" r="5399"',
            b't="10002000" d="2000" r="5399"',
        )
        session.update(
            shifted, "https://media.example/live/manifest.mpd"
        )
        refreshed = session.media_playlist(0).decode()
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:6801", refreshed)

if __name__ == "__main__": unittest.main()
