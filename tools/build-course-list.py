#!/usr/bin/env python3
"""Build courses-v1.json, the worldwide golf course list, from Overpass.

Usage:
  build-course-list.py [--input FILE] [--output FILE] [--min-courses N]

Without --input it queries Overpass as 48 bounding boxes, one at a time, each backing
off exponentially and then falling back to a second endpoint; one box failing fails the
run. The output file is replaced only when every publish check passes;
a failed check exits non-zero and leaves the old file alone.
"""

import argparse
import http.client
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BOX_QUERY = ('[out:json][timeout:300];'
             '(way["leisure"="golf_course"](%(box)s);'
             'relation["leisure"="golf_course"](%(box)s););'
             'out tags center;')
LAT_BANDS = 6
LON_BANDS = 8
BOX_PAUSE = 5
ENDPOINTS = ["https://overpass-api.de/api/interpreter",
             "https://overpass.private.coffee/api/interpreter"]
USER_AGENT = "RoundReplay course list builder (austinhect@gmail.com)"
RETRY_CODES = (429, 502, 503, 504)
ATTEMPTS_PER_ENDPOINT = 6
BOX_ATTEMPTS_PER_ENDPOINT = 4
FIRST_BACKOFF = 30
REQUEST_TIMEOUT = 400
ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0"
DEFAULT_OUTPUT = "roundreplay/courses-v1.json"
DEFAULT_MIN_COURSES = 30000
MAX_SHRINK = 0.95


class FetchError(Exception):
    pass


class PublishError(Exception):
    pass


class BadBody(Exception):
    """A response that arrived but must be retried."""


def fetch_overpass(query, opener=urllib.request.urlopen, sleep=time.sleep,
                   attempts=ATTEMPTS_PER_ENDPOINT, validate=None, on_attempt=None):
    """POST the query; returns the response body bytes or raises FetchError.

    validate(body) may raise BadBody to make the attempt count as retryable.
    on_attempt() is called before each request.
    """
    payload = urllib.parse.urlencode({"data": query}).encode("utf-8")
    last = None
    for url in ENDPOINTS:
        delay = FIRST_BACKOFF
        for attempt in range(attempts):
            request = urllib.request.Request(
                url, data=payload, headers={"User-Agent": USER_AGENT})
            if on_attempt:
                on_attempt()
            try:
                with opener(request, timeout=REQUEST_TIMEOUT) as reply:
                    body = reply.read()
                if validate:
                    validate(body)
                return body
            except urllib.error.HTTPError as error:
                last = "%s: HTTP %d" % (url, error.code)
                if error.code not in RETRY_CODES:
                    break
            except (urllib.error.URLError, http.client.HTTPException, OSError,
                    BadBody) as error:
                last = "%s: %s" % (url, error)
            print("fetch failed (%s)" % last, file=sys.stderr)
            if attempt < attempts - 1:
                sleep(delay)
                delay *= 2
    raise FetchError("all endpoints failed; last error: %s" % last)


def grid_boxes():
    """The 48 (south, west, north, east) boxes covering the globe, row by row."""
    lat_step = 180 // LAT_BANDS
    lon_step = 360 // LON_BANDS
    return [(-90 + i * lat_step, -180 + j * lon_step,
             -90 + (i + 1) * lat_step, -180 + (j + 1) * lon_step)
            for i in range(LAT_BANDS) for j in range(LON_BANDS)]


def check_box_body(body):
    """Raises BadBody for a box response that cannot be used; returns the parsed data."""
    try:
        data = json.loads(body)
    except ValueError as error:
        raise BadBody("response is not JSON: %s" % error)
    if not isinstance(data, dict):
        raise BadBody("response is not a JSON object")
    if data.get("remark"):
        raise BadBody("server remark: %s" % data["remark"])
    if not isinstance(data.get("elements"), list):
        raise BadBody("response has no elements")
    return data


def fetch_world(opener=urllib.request.urlopen, sleep=time.sleep):
    """Query every box in turn; returns one merged response-shaped dict.

    All or nothing: a box that fails every attempt raises FetchError naming it.
    """
    boxes = grid_boxes()
    merged = {}
    bases = []
    for index, box in enumerate(boxes, 1):
        if index > 1:
            sleep(BOX_PAUSE)
        label = "box %d/%d (%s)" % (index, len(boxes), ",".join(str(v) for v in box))
        query = BOX_QUERY % {"box": ",".join(str(v) for v in box)}
        attempts = []
        started = time.monotonic()
        try:
            body = fetch_overpass(query, opener=opener, sleep=sleep,
                                  attempts=BOX_ATTEMPTS_PER_ENDPOINT,
                                  validate=check_box_body,
                                  on_attempt=lambda: attempts.append(1))
        except FetchError as error:
            raise FetchError("%s failed: %s" % (label, error))
        data = check_box_body(body)
        for e in data["elements"]:
            merged[(e.get("type"), e.get("id"))] = e
        base = (data.get("osm3s") or {}).get("timestamp_osm_base", "")
        if base:
            bases.append(base)
        print("%s: %d elements, %d attempts, %.1f s"
              % (label, len(data["elements"]), len(attempts),
                 time.monotonic() - started))
    return {"osm3s": {"timestamp_osm_base": min(bases) if bases else ""},
            "elements": list(merged.values())}


def build_rows(elements):
    rows = []
    for e in elements:
        center = e.get("center")
        if not center:
            continue
        prefix = "w" if e.get("type") == "way" else "r"
        name = (e.get("tags") or {}).get("name", "")
        rows.append(["%s%d" % (prefix, e["id"]), name,
                     round(center["lat"], 4), round(center["lon"], 4)])
    rows.sort(key=lambda r: r[0])
    return rows


def build_document(data, generated):
    return {"format": 1,
            "generated": generated,
            "osmBase": (data.get("osm3s") or {}).get("timestamp_osm_base", ""),
            "attribution": ATTRIBUTION,
            "courses": build_rows(data["elements"])}


def serialize(document):
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


def check_publish(data, row_count, old_path, min_courses):
    """Raises PublishError unless the response is safe to publish."""
    if not isinstance(data, dict):
        raise PublishError("response is not a JSON object")
    if data.get("remark"):
        raise PublishError("server remark: %s" % data["remark"])
    if not isinstance(data.get("elements"), list):
        raise PublishError("response has no elements")
    if row_count < min_courses:
        raise PublishError("only %d courses, need at least %d" % (row_count, min_courses))
    if old_path and os.path.exists(old_path):
        try:
            with open(old_path, encoding="utf-8") as f:
                old_count = len(json.load(f)["courses"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            print("warning: cannot read %s (%s); skipping the shrink check"
                  % (old_path, error), file=sys.stderr)
            return
        if row_count < old_count * MAX_SHRINK:
            raise PublishError("%d courses is more than 5%% below the published %d"
                               % (row_count, old_count))


def write_atomic(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


def main(argv=None, opener=urllib.request.urlopen, sleep=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", help="saved Overpass response; no network")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--min-courses", type=int, default=DEFAULT_MIN_COURSES)
    args = parser.parse_args(argv)

    try:
        if args.input:
            with open(args.input, "rb") as f:
                body = f.read()
            try:
                data = json.loads(body)
            except ValueError as error:
                raise PublishError("response is not JSON: %s" % error)
        else:
            data = fetch_world(opener=opener, sleep=sleep)
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Validate shape and remark before building rows.
        check_publish(data, args.min_courses, None, args.min_courses)
        document = build_document(data, generated)
        check_publish(data, len(document["courses"]), args.output, args.min_courses)
        write_atomic(args.output, serialize(document))
    except (FetchError, PublishError, OSError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print("wrote %d courses to %s" % (len(document["courses"]), args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
