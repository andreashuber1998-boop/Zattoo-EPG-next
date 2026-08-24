#!/usr/bin/env python3
"""Expose a provider DASH DVR window as replay and on-demand HLS."""

import json
import os
import re
import threading
import math
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse, urljoin
from urllib.request import Request, urlopen

ISO_DURATION = re.compile(r"^PT(?:(?P<hours>[0-9.]+)H)?(?:(?P<minutes>[0-9.]+)M)?(?:(?P<seconds>[0-9.]+)S)?$")
CHANNEL = re.compile(r"^[A-Za-z0-9_.-]+$")


def load_channel_allowlist(path):
    if not path:
        return None
    allowed = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            value = line.split("#", 1)[0].strip()
            if value:
                if not CHANNEL.fullmatch(value):
                    raise ValueError(f"invalid replay channel in allowlist: {value!r}")
                allowed.add(value)
    return allowed


def parse_duration(value):
    match = ISO_DURATION.match(value or "")
    if not match:
        raise ValueError(f"unsupported ISO-8601 duration: {value!r}")
    return float(match.group("hours") or 0) * 3600 + float(match.group("minutes") or 0) * 60 + float(match.group("seconds") or 0)


def format_duration(seconds):
    return f"PT{seconds:.3f}S".replace(".000S", "S")


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def expand_timeline(timeline, fallback_seconds, timescale):
    entries, current = [], 0
    nodes = [node for node in timeline if local_name(node.tag) == "S"]
    for index, node in enumerate(nodes):
        duration = int(node.attrib["d"])
        start = int(node.attrib.get("t", current))
        repeat = int(node.attrib.get("r", "0"))
        if repeat < 0:
            if index + 1 < len(nodes) and "t" in nodes[index + 1].attrib:
                repeat = max(0, (int(nodes[index + 1].attrib["t"]) - start) // duration - 1)
            else:
                repeat = max(0, int(fallback_seconds * timescale) // duration - 1)
        if repeat > 20000:
            raise ValueError("DASH timeline is unexpectedly large")
        entries.extend((start + item * duration, duration) for item in range(repeat + 1))
        current = start + (repeat + 1) * duration
    if not entries:
        raise ValueError("DASH manifest contains an empty SegmentTimeline")
    return entries


def rebuild_timeline(timeline, entries):
    for child in list(timeline):
        timeline.remove(child)
    namespace = timeline.tag[:-len("SegmentTimeline")] if timeline.tag.endswith("SegmentTimeline") else ""
    index = 0
    while index < len(entries):
        start, duration = entries[index]
        end = index + 1
        while end < len(entries) and entries[end][1] == duration and entries[end][0] == entries[end - 1][0] + duration:
            end += 1
        attributes = {"d": str(duration)}
        if index == 0:
            attributes["t"] = str(start)
        if end - index > 1:
            attributes["r"] = str(end - index - 1)
        ET.SubElement(timeline, f"{namespace}S", attributes)
        index = end


class CatchupTimelineState:
    """Keep a monotonically growing DASH timeline for one channel."""

    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}
        self.presentation_offsets = {}
        self.availability_start_time = None

    def apply(self, root, fallback_seconds):
        with self.lock:
            if self.availability_start_time is None:
                self.availability_start_time = root.attrib.get(
                    "availabilityStartTime"
                )
            elif self.availability_start_time:
                root.attrib["availabilityStartTime"] = (
                    self.availability_start_time
                )

            templates = [
                node for node in root.iter()
                if local_name(node.tag) == "SegmentTemplate"
            ]
            for index, template in enumerate(templates):
                timeline = next(
                    (
                        node for node in template
                        if local_name(node.tag) == "SegmentTimeline"
                    ),
                    None,
                )
                if timeline is None:
                    continue
                timescale = int(template.attrib.get("timescale", "1"))
                incoming = expand_timeline(
                    timeline, fallback_seconds, timescale
                )
                if index not in self.entries:
                    self.entries[index] = incoming
                    self.presentation_offsets[index] = template.attrib.get(
                        "presentationTimeOffset"
                    )
                else:
                    known = self.entries[index]
                    last_end = known[-1][0] + known[-1][1]
                    # A refreshed dynamic MPD may still contain old segments.
                    # Append only segments strictly after the cached timeline.
                    known.extend(
                        entry for entry in incoming
                        if entry[0] >= last_end
                    )
                offset = self.presentation_offsets[index]
                if offset is None:
                    template.attrib.pop("presentationTimeOffset", None)
                else:
                    template.attrib["presentationTimeOffset"] = offset
                rebuild_timeline(timeline, self.entries[index])

            if not self.entries:
                raise ValueError("DASH manifest has no SegmentTimeline")


def substitute_dash_template(
    value, representation_id, bandwidth, segment_time=None
):
    value = value.replace("$RepresentationID$", representation_id)
    value = value.replace("$Bandwidth$", str(bandwidth))
    if segment_time is not None:
        value = value.replace("$Time$", str(segment_time))
    return value.replace("$$", "$")


class DashHlsSession:
    """Translate one DASH DVR window into sliding fMP4 HLS playlists."""

    def __init__(self, channel, window_seconds=7200, delay_seconds=10):
        self.channel = channel
        self.window_seconds = window_seconds
        self.delay_seconds = delay_seconds
        self.lock = threading.Lock()
        self.tracks = []

    def update(self, source, source_url):
        root = ET.fromstring(source)
        if local_name(root.tag) != "MPD":
            raise ValueError("upstream response is not a DASH MPD")
        upstream_window = parse_duration(root.attrib.get("timeShiftBufferDepth", ""))
        if self.window_seconds > upstream_window:
            raise ValueError("requested HLS window exceeds upstream window")
        tracks = []
        for adaptation in (node for node in root.iter() if local_name(node.tag) == "AdaptationSet"):
            adaptation_template = next((node for node in adaptation if local_name(node.tag) == "SegmentTemplate"), None)
            for representation in (node for node in adaptation if local_name(node.tag) == "Representation"):
                template = next((node for node in representation if local_name(node.tag) == "SegmentTemplate"), adaptation_template)
                if template is None:
                    continue
                timeline = next((node for node in template if local_name(node.tag) == "SegmentTimeline"), None)
                if timeline is None:
                    continue
                timescale = int(template.attrib.get("timescale", "1"))
                entries = expand_timeline(timeline, upstream_window, timescale)
                end = entries[-1][0] + entries[-1][1]
                start_limit = end - self.window_seconds * timescale
                entries = [item for item in entries if item[0] + item[1] > start_limit]
                mime = representation.attrib.get("mimeType") or adaptation.attrib.get("mimeType", "")
                content_type = representation.attrib.get("contentType") or adaptation.attrib.get("contentType", "")
                kind = "audio" if content_type == "audio" or mime.startswith("audio/") else "video"
                base = next(((node.text or "").strip() for node in representation if local_name(node.tag) == "BaseURL"), source_url)
                tracks.append({
                    "kind": kind,
                    "id": representation.attrib.get("id", str(len(tracks))),
                    "bandwidth": int(representation.attrib.get("bandwidth", "1")),
                    "codecs": representation.attrib.get("codecs") or adaptation.attrib.get("codecs", ""),
                    "width": representation.attrib.get("width"),
                    "height": representation.attrib.get("height"),
                    "frame_rate": representation.attrib.get("frameRate") or adaptation.attrib.get("frameRate"),
                    "timescale": timescale,
                    "entries": entries,
                    "media": urljoin(base, template.attrib["media"]),
                    "initialization": urljoin(base, template.attrib["initialization"]),
                })
        if not tracks:
            raise ValueError("DASH manifest has no playable representations")
        with self.lock:
            self.tracks = tracks

    def master_playlist(self):
        with self.lock:
            videos = [(index, track) for index, track in enumerate(self.tracks) if track["kind"] == "video"]
            audios = [(index, track) for index, track in enumerate(self.tracks) if track["kind"] == "audio"]
            lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
            query = (
                f"?window={self.window_seconds}"
                f"&delay={self.delay_seconds}"
            )
            if audios:
                lines.append('#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Default",DEFAULT=YES,AUTOSELECT=YES,' f'URI="track-{audios[0][0]}.m3u8{query}"')
            for index, track in videos:
                attrs = [f'BANDWIDTH={track["bandwidth"]}']
                if track["codecs"]:
                    codecs = track["codecs"]
                    if audios and audios[0][1]["codecs"]:
                        codecs += "," + audios[0][1]["codecs"]
                    attrs.append(f'CODECS="{codecs}"')
                if track["width"] and track["height"]:
                    attrs.append(f'RESOLUTION={track["width"]}x{track["height"]}')
                if track["frame_rate"]:
                    attrs.append(f'FRAME-RATE={track["frame_rate"]}')
                if audios:
                    attrs.append('AUDIO="audio"')
                lines.extend([
                    "#EXT-X-STREAM-INF:" + ",".join(attrs),
                    f"track-{index}.m3u8{query}",
                ])
            return ("\n".join(lines) + "\n").encode()

    def media_playlist(self, index):
        with self.lock:
            track = self.tracks[index]
            entries = track["entries"]
            if not entries:
                raise ValueError("track has no segments")
            # The provider may publish several 1.6-second segments in batches
            # roughly every six seconds. A two-second target makes FFmpeg
            # exhaust its unchanged-playlist retries before the next batch.
            target = max(
                8,
                math.ceil(
                    max(duration for _, duration in entries)
                    / track["timescale"]
                ),
            )
            # HLS clients use MEDIA-SEQUENCE to recognize new segments in a
            # refreshed sliding playlist. Keeping this at zero makes FFmpeg
            # stop at the live edge after only a few seconds.
            first_start, first_duration = entries[0]
            media_sequence = first_start // first_duration
            query = (
                f"?window={self.window_seconds}"
                f"&delay={self.delay_seconds}"
            )
            lines = [
                "#EXTM3U", "#EXT-X-VERSION:7",
                f"#EXT-X-TARGETDURATION:{target}",
                f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}",
                f"#EXT-X-START:TIME-OFFSET=-{self.delay_seconds},PRECISE=NO",
                f'#EXT-X-MAP:URI="track-{index}/init.mp4{query}"',
            ]
            for start, duration in entries:
                lines.extend([
                    f"#EXTINF:{duration / track['timescale']:.6f},",
                    f"track-{index}/segment-{start}.m4s{query}",
                ])
            return ("\n".join(lines) + "\n").encode()

    def segment_url(self, index, segment_time=None):
        with self.lock:
            track = self.tracks[index]
            template = track["initialization"] if segment_time is None else track["media"]
            return substitute_dash_template(
                template, track["id"], track["bandwidth"], segment_time
            )


def build_replay_mpd(source, offset_seconds):
    root = ET.fromstring(source)
    if local_name(root.tag) != "MPD":
        raise ValueError("upstream response is not a DASH MPD")
    # FFmpeg's DASH probe expects the regular <MPD xmlns="..."> spelling.
    # ElementTree otherwise serializes it as <ns0:MPD>, which is valid XML
    # but is not recognized as DASH during format probing.
    if root.tag.startswith("{"):
        ET.register_namespace("", root.tag[1:].split("}", 1)[0])
    window_seconds = parse_duration(root.attrib.get("timeShiftBufferDepth", ""))
    if offset_seconds < 1 or offset_seconds > window_seconds:
        raise ValueError(f"offset must be between 1 and {int(window_seconds)} seconds")
    root.attrib["type"] = "static"
    for attribute in ("availabilityStartTime", "publishTime", "minimumUpdatePeriod", "timeShiftBufferDepth", "suggestedPresentationDelay"):
        root.attrib.pop(attribute, None)
    actual_durations = []
    for template in (node for node in root.iter() if local_name(node.tag) == "SegmentTemplate"):
        timeline = next((node for node in template if local_name(node.tag) == "SegmentTimeline"), None)
        if timeline is None:
            continue
        timescale = int(template.attrib.get("timescale", "1"))
        entries = expand_timeline(timeline, window_seconds, timescale)
        end_time = entries[-1][0] + entries[-1][1]
        wanted_start = end_time - offset_seconds * timescale
        selected_index = max((i for i, (start, _) in enumerate(entries) if start <= wanted_start), default=0)
        selected = entries[selected_index:]
        template.attrib["presentationTimeOffset"] = str(selected[0][0])
        rebuild_timeline(timeline, selected)
        actual_durations.append((end_time - selected[0][0]) / timescale)
    if not actual_durations:
        raise ValueError("DASH manifest has no SegmentTimeline")
    replay_duration = min(actual_durations)
    root.attrib["mediaPresentationDuration"] = format_duration(replay_duration)
    for period in (node for node in root.iter() if local_name(node.tag) == "Period"):
        period.attrib["start"] = "PT0S"
        period.attrib["duration"] = format_duration(replay_duration)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_timeshift_mpd(source, window_seconds=7200, delay_seconds=10):
    """Keep the MPD dynamic, but advertise a bounded DVR window and live delay."""
    root = ET.fromstring(source)
    if local_name(root.tag) != "MPD":
        raise ValueError("upstream response is not a DASH MPD")
    if root.tag.startswith("{"):
        ET.register_namespace("", root.tag[1:].split("}", 1)[0])
    upstream_window = parse_duration(root.attrib.get("timeShiftBufferDepth", ""))
    if window_seconds < 1 or window_seconds > upstream_window:
        raise ValueError(f"window must be between 1 and {int(upstream_window)} seconds")
    if delay_seconds < 1 or delay_seconds >= window_seconds:
        raise ValueError("delay must be at least 1 second and smaller than the window")
    root.attrib["type"] = "dynamic"
    root.attrib["timeShiftBufferDepth"] = format_duration(window_seconds)
    root.attrib["suggestedPresentationDelay"] = format_duration(delay_seconds)
    root.attrib.pop("mediaPresentationDuration", None)
    for period in (node for node in root.iter() if local_name(node.tag) == "Period"):
        period.attrib.pop("duration", None)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_catchup_mpd(
    source, window_seconds=7200, delay_seconds=10, state=None
):
    """Expose the upstream timeline as a sliding window that grows at live edge."""
    root = ET.fromstring(source)
    if local_name(root.tag) != "MPD":
        raise ValueError("upstream response is not a DASH MPD")
    if root.tag.startswith("{"):
        ET.register_namespace("", root.tag[1:].split("}", 1)[0])
    upstream_window = parse_duration(root.attrib.get("timeShiftBufferDepth", ""))
    if window_seconds < 1 or window_seconds > upstream_window:
        raise ValueError(f"window must be between 1 and {int(upstream_window)} seconds")
    if delay_seconds < 1 or delay_seconds >= window_seconds:
        raise ValueError("delay must be at least 1 second and smaller than the window")

    if state is None:
        state = CatchupTimelineState()
    state.apply(root, upstream_window)

    # Keep the timeline origin stable and only append new segments. FFmpeg
    # otherwise treats an overlapping refreshed audio timeline as new input
    # and emits thousands of backward DTS timestamps.
    root.attrib["type"] = "dynamic"
    root.attrib["timeShiftBufferDepth"] = format_duration(window_seconds)
    root.attrib["suggestedPresentationDelay"] = format_duration(delay_seconds)
    root.attrib["minimumUpdatePeriod"] = "PT2S"
    root.attrib["publishTime"] = (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    root.attrib.pop("mediaPresentationDuration", None)
    for period in (node for node in root.iter() if local_name(node.tag) == "Period"):
        period.attrib.pop("duration", None)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_player_page(channel, window_seconds=7200, delay_seconds=30):
    """Build a same-origin HLS.js player with two-hour DVR controls."""
    stream_url = (
        f"/hls/{quote(channel)}/master.m3u8"
        f"?window={window_seconds}&delay={delay_seconds}"
    )
    channel_json = json.dumps(channel)
    stream_json = json.dumps(stream_url)
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TV Replay – {channel}</title>
<style>
:root {{ color-scheme: dark; font-family: system-ui, sans-serif; }}
body {{ margin: 0; background: #101114; color: #fff; }}
main {{ width: min(1200px, 100%); margin: auto; padding: 16px; box-sizing: border-box; }}
video {{ width: 100%; max-height: 75vh; background: #000; }}
.controls {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }}
button {{ border: 0; border-radius: 6px; padding: 10px 14px; font-weight: 650; cursor: pointer; }}
.live {{ background: #e53935; color: #fff; }}
.status {{ margin-top: 10px; color: #c8cad0; font-variant-numeric: tabular-nums; }}
.error {{ color: #ff8a80; }}
</style>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
</head>
<body>
<main>
<h1 id="title"></h1>
<video id="video" controls playsinline></video>
<div class="controls">
<button data-back="7200">−2 Stunden</button>
<button data-back="3600">−1 Stunde</button>
<button data-back="1800">−30 Minuten</button>
<button data-back="600">−10 Minuten</button>
<button class="live" id="live">Live</button>
</div>
<div class="status" id="status">Stream wird geladen …</div>
</main>
<script>
const channel = {channel_json};
const source = {stream_json};
const configuredDelay = {int(delay_seconds)};
const video = document.getElementById("video");
const status = document.getElementById("status");
document.getElementById("title").textContent = "TV Replay – " + channel;

function range() {{
  if (!video.seekable.length) return null;
  const last = video.seekable.length - 1;
  return {{start: video.seekable.start(0), end: video.seekable.end(last)}};
}}
function seekBack(seconds) {{
  const current = range();
  if (!current) return;
  video.currentTime = Math.max(current.start, current.end - seconds);
  video.play().catch(() => {{}});
}}
function goLive() {{
  const current = range();
  if (!current) return;
  video.currentTime = Math.max(current.start, current.end - configuredDelay);
  video.play().catch(() => {{}});
}}
function clock(seconds) {{
  seconds = Math.max(0, Math.round(seconds));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  return [hours, minutes, secs].map(value => String(value).padStart(2, "0")).join(":");
}}
function updateStatus() {{
  const current = range();
  if (!current) {{
    status.textContent = "Puffer wird aufgebaut …";
    return;
  }}
  const behind = current.end - video.currentTime;
  const available = current.end - current.start;
  status.textContent = "Verfügbar: " + clock(available) +
    " · hinter Live: " + clock(behind) +
    " · gepuffert: " + clock(video.buffered.length ?
      video.buffered.end(video.buffered.length - 1) - video.currentTime : 0);
}}
document.querySelectorAll("[data-back]").forEach(button => {{
  button.addEventListener("click", () => seekBack(Number(button.dataset.back)));
}});
document.getElementById("live").addEventListener("click", goLive);
video.addEventListener("timeupdate", updateStatus);
video.addEventListener("progress", updateStatus);

if (window.Hls && Hls.isSupported()) {{
  const hls = new Hls({{
    liveSyncDuration: configuredDelay,
    liveMaxLatencyDuration: configuredDelay + 30,
    maxBufferLength: 90,
    maxMaxBufferLength: 180,
    backBufferLength: 7200,
    liveDurationInfinity: true,
    enableWorker: true
  }});
  hls.loadSource(source);
  hls.attachMedia(video);
  hls.on(Hls.Events.MANIFEST_PARSED, () => {{
    goLive();
    updateStatus();
  }});
  hls.on(Hls.Events.ERROR, (_event, data) => {{
    if (data.fatal) {{
      status.classList.add("error");
      status.textContent = "Playerfehler: " + data.details;
    }}
  }});
}} else if (video.canPlayType("application/vnd.apple.mpegurl")) {{
  video.src = source;
  video.addEventListener("loadedmetadata", goLive, {{once: true}});
}} else {{
  status.classList.add("error");
  status.textContent = "Dieser Browser unterstützt HLS nicht.";
}}
</script>
</body>
</html>
""".encode()


class ReplayHandler(BaseHTTPRequestHandler):
    upstream = os.environ.get("TELERISING_BASE_URL", "").rstrip("/")
    timeout = float(os.environ.get("REPLAY_UPSTREAM_TIMEOUT", "15"))
    allowlist = load_channel_allowlist(os.environ.get("REPLAY_CHANNEL_FILTER_FILE", ""))
    catchup_states = {}
    catchup_states_lock = threading.Lock()
    hls_sessions = {}
    hls_sessions_lock = threading.Lock()

    @classmethod
    def catchup_state(cls, channel):
        with cls.catchup_states_lock:
            return cls.catchup_states.setdefault(
                channel, CatchupTimelineState()
            )

    @classmethod
    def hls_session(cls, channel, window, delay):
        key = (channel, window, delay)
        with cls.hls_sessions_lock:
            return cls.hls_sessions.setdefault(
                key, DashHlsSession(channel, window, delay)
            )

    def fetch_upstream(self, channel):
        source_url = f"{self.upstream}/api/zc2/live/{quote(channel)}"
        request = Request(
            source_url,
            headers={"User-Agent": "DASH-HLS-Gateway/1.0"},
        )
        with urlopen(request, timeout=self.timeout) as response:
            return response.read(), response.geturl()

    def send_bytes(self, output, content_type):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(output)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(output)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_response(HTTPStatus.OK); self.end_headers(); self.wfile.write(b"ok\n"); return
        player = re.fullmatch(r"/player/([^/]+)", parsed.path)
        if player:
            channel = player.group(1)
            if not CHANNEL.fullmatch(channel):
                self.send_error(HTTPStatus.NOT_FOUND); return
            if self.allowlist is not None and channel not in self.allowlist:
                self.send_error(HTTPStatus.NOT_FOUND); return
            try:
                query = parse_qs(parsed.query)
                window = int(query.get("window", ["7200"])[0])
                delay = int(query.get("delay", ["30"])[0])
                if window < 60 or delay < 1 or delay >= window:
                    raise ValueError("invalid player window or delay")
                output = build_player_page(channel, window, delay)
            except ValueError as error:
                self.send_error(HTTPStatus.BAD_REQUEST, str(error)); return
            self.send_bytes(output, "text/html; charset=utf-8")
            return
        hls = re.fullmatch(
            r"/hls/([^/]+)/(master|track-(\d+))\.m3u8", parsed.path
        )
        segment = re.fullmatch(
            r"/hls/([^/]+)/track-(\d+)/(init\.mp4|segment-(\d+)\.m4s)",
            parsed.path,
        )
        if hls or segment:
            channel = (hls or segment).group(1)
            if not CHANNEL.fullmatch(channel):
                self.send_error(HTTPStatus.NOT_FOUND); return
            if self.allowlist is not None and channel not in self.allowlist:
                self.send_error(HTTPStatus.NOT_FOUND); return
            query = parse_qs(parsed.query)
            window = int(query.get("window", ["7200"])[0])
            delay = int(query.get("delay", ["10"])[0])
            session = self.hls_session(channel, window, delay)
            try:
                if hls:
                    source, source_url = self.fetch_upstream(channel)
                    session.update(source, source_url)
                    output = session.master_playlist() if hls.group(2) == "master" else session.media_playlist(int(hls.group(3)))
                    self.send_bytes(output, "application/vnd.apple.mpegurl")
                else:
                    index = int(segment.group(2))
                    segment_time = None if segment.group(3) == "init.mp4" else int(segment.group(4))
                    request = Request(
                        session.segment_url(index, segment_time),
                        headers={"User-Agent": "DASH-HLS-Gateway/1.0"},
                    )
                    with urlopen(request, timeout=self.timeout) as response:
                        output = response.read()
                        content_type = response.headers.get_content_type()
                    self.send_bytes(output, content_type)
            except (ValueError, IndexError, HTTPError, URLError, TimeoutError) as error:
                self.send_error(HTTPStatus.BAD_GATEWAY, str(error))
            return
        match = re.fullmatch(r"/(replay|timeshift|catchup)/([^/]+)\.mpd", parsed.path)
        if not match or not CHANNEL.fullmatch(match.group(2)):
            self.send_error(HTTPStatus.NOT_FOUND); return
        if self.allowlist is not None and match.group(2) not in self.allowlist:
            self.send_error(HTTPStatus.NOT_FOUND); return
        try:
            if not self.upstream:
                raise ValueError("TELERISING_BASE_URL is not configured")
            query = parse_qs(parsed.query)
            source, _ = self.fetch_upstream(match.group(2))
            if match.group(1) == "replay":
                output = build_replay_mpd(source, int(query.get("offset", ["7200"])[0]))
            elif match.group(1) == "catchup":
                output = build_catchup_mpd(
                    source,
                    int(query.get("window", ["7200"])[0]),
                    int(query.get("delay", ["10"])[0]),
                    self.catchup_state(match.group(2)),
                )
            else:
                output = build_timeshift_mpd(
                    source,
                    int(query.get("window", ["7200"])[0]),
                    int(query.get("delay", ["10"])[0]),
                )
        except (ValueError, HTTPError, URLError, TimeoutError) as error:
            self.send_error(HTTPStatus.BAD_GATEWAY, str(error)); return
        self.send_bytes(output, "application/dash+xml")


def main():
    port = int(os.environ.get("REPLAY_PORT", "8090"))
    print(f"Replay proxy listening on port {port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), ReplayHandler).serve_forever()


if __name__ == "__main__":
    main()
