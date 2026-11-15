#!/usr/bin/env python3
"""Studylog: BAND Open API posting and one-comment daily leave summary.

No network access is made in preview mode. Live actions are explicitly gated
by BAND_LIVE_ENABLED=true, and should only be enabled after BAND API approval.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")
CATEGORIES = ("반휴", "월휴", "시험휴")
BASE = "https://openapi.band.us"
MAX_PAGES = 30


class StudylogError(Exception):
    """Safe-to-display operational error without credentials."""


def sunday_for(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=(day.weekday() + 1) % 7)


def weekly_dates(sunday: dt.date) -> list[dt.date]:
    if sunday.weekday() != 6:
        raise ValueError("The input date must be Sunday.")
    return [sunday + dt.timedelta(days=i) for i in range(1, 7)]


def post_text(day: dt.date) -> str:
    return f"{day:%y/%m/%d} {WEEKDAYS[day.weekday()]}"


def cutoff_for(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(7, 20), tzinfo=KST)


def from_band_time(value: int | str) -> dt.datetime:
    # BAND documents created_at as epoch milliseconds.
    number = int(value)
    return dt.datetime.fromtimestamp(number / 1000, tz=dt.timezone.utc).astimezone(KST)


def comment_body(comment: dict) -> str:
    return str(comment.get("content") or comment.get("body") or "").strip()


def is_summary(body: str) -> bool:
    lines = body.strip().splitlines()
    if len(lines) != 3:
        return False
    return all(re.match(r"^\s*" + re.escape(cat) + r"\s*:", line) for cat, line in zip(CATEGORIES, lines))


def aggregate(comments: list[dict], day: dt.date) -> str:
    deadline = cutoff_for(day)
    names = {key: [] for key in CATEGORIES}
    seen = {key: set() for key in CATEGORIES}
    # Sort for stable output regardless of API ordering/pagination.
    ordered = sorted(comments, key=lambda x: int(x.get("created_at", 0)))
    for comment in ordered:
        category = comment_body(comment)
        if category not in names:
            continue
        if "created_at" not in comment:
            # Missing timestamps must not silently be treated as on time.
            raise StudylogError("A leave comment is missing its creation timestamp.")
        if from_band_time(comment["created_at"]) > deadline:
            continue
        author = comment.get("author") or {}
        name = str(author.get("name") or "").strip()
        if not name:
            raise StudylogError("A leave comment is missing its author name.")
        # The stable BAND user key is preferred; name is the fallback.
        identity = str(author.get("user_key") or name)
        if identity not in seen[category]:
            seen[category].add(identity)
            names[category].append(name)
    return "\n".join(f"{cat}: {', '.join(names[cat]) if names[cat] else '-'}" for cat in CATEGORIES)


def matching_post(posts: list[dict], day: dt.date) -> dict | None:
    """Find an exact date-title post created within the target calendar week.

    Ambiguous matches are unsafe: do not write to either post.
    """
    week_start = sunday_for(day)
    matches = []
    for post in posts:
        if str(post.get("content") or "").strip() != post_text(day):
            continue
        if not post.get("post_key"):
            continue
        if "created_at" not in post:
            raise StudylogError("A matching post has no timestamp; cannot verify its week.")
        creation_day = from_band_time(post["created_at"]).date()
        if week_start <= creation_day <= week_start + dt.timedelta(days=6):
            matches.append(post)
    if len(matches) > 1:
        raise StudylogError(f"Multiple matching posts exist for {post_text(day)}; skipped for safety.")
    return matches[0] if matches else None


class BandAPI:
    def __init__(self, token: str, band_key: str):
        self.token = token
        self.band_key = band_key

    def request(self, method: str, path: str, params: dict) -> dict:
        data = {**params, "band_key": self.band_key, "access_token": self.token}
        body = urlencode(data).encode("utf-8")
        if method == "GET":
            req = Request(BASE + path + "?" + body.decode("ascii"), method="GET")
        else:
            req = Request(BASE + path, data=body, method="POST")
        try:
            with urlopen(req, timeout=20) as response:
                result = json.load(response)
        except HTTPError as ex:
            raise StudylogError(f"BAND API HTTP error {ex.code}.") from None
        except (URLError, TimeoutError, OSError) as ex:
            raise StudylogError(f"BAND API connection failed ({type(ex).__name__}).") from None
        except (ValueError, TypeError):
            raise StudylogError("BAND API returned an unreadable response.") from None
        if result.get("result_code") != 1:
            raise StudylogError(f"BAND API returned result_code={result.get('result_code')}.")
        return result.get("result_data") or {}

    def ensure_permissions(self, required: str) -> None:
        response = self.request("GET", "/v2/band/permissions", {"permissions": required})
        allowed = response.get("permissions", response.get("permission", []))
        if isinstance(allowed, dict):
            allowed = list(allowed.keys())
        required_set = set(required.split(","))
        if not required_set.issubset(set(allowed or [])):
            raise StudylogError("BAND account does not have the required posting/commenting permission.")

    def paginated(self, path: str, params: dict) -> list[dict]:
        items: list[dict] = []
        next_params = dict(params)
        visited: set[str] = set()
        for _ in range(MAX_PAGES):
            response = self.request("GET", path, next_params)
            items.extend(response.get("items") or [])
            cursor_data = (response.get("paging") or {}).get("next_params")
            if not cursor_data:
                return items
            after = str(cursor_data.get("after") or "")
            if not after or after in visited:
                raise StudylogError("BAND pagination cursor is invalid or repeating.")
            visited.add(after)
            # Never trust a returned token or target band from pagination.
            next_params = {**params, "after": after}
            if cursor_data.get("limit"):
                next_params["limit"] = cursor_data["limit"]
        raise StudylogError("BAND pagination safety limit reached; no posts/comments were written.")

    def posts(self) -> list[dict]:
        return self.paginated("/v2/band/posts", {"locale": "ko_KR"})

    def comments(self, post_key: str) -> list[dict]:
        return self.paginated("/v2/band/post/comments", {"post_key": post_key, "sort": "+created_at"})

    def create_post(self, content: str) -> None:
        self.request("POST", "/v2.2/band/post/create", {"content": content, "do_push": "false"})

    def create_comment(self, post_key: str, body: str) -> None:
        self.request("POST", "/v2/band/post/comment/create", {"post_key": post_key, "body": body})


def publish(api: BandAPI, sunday: dt.date) -> None:
    api.ensure_permissions("posting")
    posts = api.posts()
    for day in weekly_dates(sunday):
        if matching_post(posts, day) is not None:
            print(f"Skip existing: {post_text(day)}")
            continue
        # A failed POST is never automatically retried, to avoid duplicate writes.
        api.create_post(post_text(day))
        print(f"Created: {post_text(day)}")


def collect(api: BandAPI, day: dt.date) -> None:
    api.ensure_permissions("commenting")
    post = matching_post(api.posts(), day)
    if not post:
        raise StudylogError(f"Post not found for {post_text(day)}. No comment created.")
    comments = api.comments(str(post["post_key"]))
    if any(is_summary(comment_body(c)) for c in comments):
        print("Existing summary found. No second summary posted.")
        return
    result = aggregate(comments, day)
    # Do not print member names to GitHub Actions logs.
    api.create_comment(str(post["post_key"]), result)
    print(f"Summary posted to {post_text(day)}.")


def validate_schedule(mode: str, moment: dt.datetime) -> bool:
    local = moment.astimezone(KST)
    if mode == "publish":
        return local.weekday() == 6 and local.time() >= dt.time(9, 0)
    if mode == "collect":
        return local.weekday() < 6 and local.time() >= dt.time(7, 20)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Studylog weekly posts and leave summary")
    parser.add_argument("--mode", choices=("auto", "preview", "publish", "collect"), default="auto")
    args = parser.parse_args()
    now = dt.datetime.now(KST)
    day = now.date()
    if args.mode == "preview":
        sunday = sunday_for(day)
        print("Sunday:", sunday.isoformat())
        print("Weekly posts:", " | ".join(post_text(x) for x in weekly_dates(sunday)))
        sample = [
            {"content": "반휴", "created_at": int(dt.datetime.combine(day, dt.time(7, 10), KST).timestamp() * 1000), "author": {"name": "김철수"}},
            {"content": "월휴", "created_at": int(dt.datetime.combine(day, dt.time(7, 15), KST).timestamp() * 1000), "author": {"name": "홍길동"}},
        ]
        print("Sample summary:\n" + aggregate(sample, day))
        return 0

    mode = args.mode
    if mode == "auto":
        mode = "publish" if day.weekday() == 6 else "collect"
    if not validate_schedule(mode, now):
        print(f"Not scheduled for {mode} on {day.isoformat()} at {now:%H:%M} KST; skipped.")
        return 0
    # Hard safety gate: an unapproved app cannot attempt real writes.
    if os.getenv("BAND_LIVE_ENABLED", "").strip().lower() != "true":
        print("BAND_LIVE_ENABLED is not true. Live BAND access disabled; skipped.")
        return 0
    token = os.getenv("BAND_ACCESS_TOKEN", "")
    band_key = os.getenv("BAND_KEY", "")
    if not token or not band_key:
        raise StudylogError("Missing BAND_ACCESS_TOKEN or BAND_KEY.")
    api = BandAPI(token, band_key)
    if mode == "publish":
        publish(api, day)
    else:
        collect(api, day)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StudylogError as exc:
        print(f"Studylog error: {exc}", file=sys.stderr)
        sys.exit(1)
