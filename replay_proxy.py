#!/usr/bin/env python3
"""Turn a dynamic Zattoo DASH window into a static replay MPD."""

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
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


def build_catchup_mpd(source, window_seconds=7200, delay_seconds=10):
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

    # Keep the upstream SegmentTimeline byte-for-byte equivalent. Rebuilding a
    # sliding dynamic timeline can make FFmpeg reread already consumed audio
    # segments after every manifest refresh, producing backward DTS jumps.
    root.attrib["type"] = "dynamic"
    root.attrib["timeShiftBufferDepth"] = format_duration(window_seconds)
    root.attrib["suggestedPresentationDelay"] = format_duration(delay_seconds)
    root.attrib["minimumUpdatePeriod"] = "PT2S"
    root.attrib["publishTime"] = (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    root.attrib.pop("mediaPresentationDuration", None)
    if not any(local_name(node.tag) == "SegmentTimeline" for node in root.iter()):
        raise ValueError("DASH manifest has no SegmentTimeline")
    for period in (node for node in root.iter() if local_name(node.tag) == "Period"):
        period.attrib.pop("duration", None)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)

class ReplayHandler(BaseHTTPRequestHandler):
    upstream = os.environ.get("TELERISING_BASE_URL", "").rstrip("/")
    timeout = float(os.environ.get("REPLAY_UPSTREAM_TIMEOUT", "15"))
    allowlist = load_channel_allowlist(os.environ.get("REPLAY_CHANNEL_FILTER_FILE", ""))

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_response(HTTPStatus.OK); self.end_headers(); self.wfile.write(b"ok\n"); return
        match = re.fullmatch(r"/(replay|timeshift|catchup)/([^/]+)\.mpd", parsed.path)
        if not match or not CHANNEL.fullmatch(match.group(2)):
            self.send_error(HTTPStatus.NOT_FOUND); return
        if self.allowlist is not None and match.group(2) not in self.allowlist:
            self.send_error(HTTPStatus.NOT_FOUND); return
        try:
            if not self.upstream:
                raise ValueError("TELERISING_BASE_URL is not configured")
            query = parse_qs(parsed.query)
            request = Request(
                f"{self.upstream}/api/zc2/live/{quote(match.group(2))}",
                headers={"User-Agent": "Zattoo-EPG-Replay-Proxy/1.0"},
            )
            with urlopen(request, timeout=self.timeout) as response:
                source = response.read()
            if match.group(1) == "replay":
                output = build_replay_mpd(source, int(query.get("offset", ["7200"])[0]))
            elif match.group(1) == "catchup":
                output = build_catchup_mpd(
                    source,
                    int(query.get("window", ["7200"])[0]),
                    int(query.get("delay", ["10"])[0]),
                )
            else:
                output = build_timeshift_mpd(
                    source,
                    int(query.get("window", ["7200"])[0]),
                    int(query.get("delay", ["10"])[0]),
                )
        except (ValueError, HTTPError, URLError, TimeoutError) as error:
            self.send_error(HTTPStatus.BAD_GATEWAY, str(error)); return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/dash+xml")
        self.send_header("Content-Length", str(len(output)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(output)


def main():
    port = int(os.environ.get("REPLAY_PORT", "8090"))
    print(f"Replay proxy listening on port {port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), ReplayHandler).serve_forever()


if __name__ == "__main__":
    main()
