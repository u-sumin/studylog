#!/usr/bin/env python3
"""Studylog: write-only weekly BAND posts. No member, post or comment reads."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")
BAND_URL = "https://openapi.band.us"
GITHUB_URL = "https://api.github.com"


class StudylogError(Exception):
    pass


def sunday_for(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=(day.weekday() + 1) % 7)


def weekly_posts(sunday: dt.date) -> list[str]:
    if sunday.weekday() != 6:
        raise ValueError("Expected Sunday.")
    return [
        f"{sunday + dt.timedelta(days=i):%y/%m/%d} {WEEKDAYS[(sunday + dt.timedelta(days=i)).weekday()]}"
        for i in range(1, 7)
    ]


def scheduled_now(now: dt.datetime) -> bool:
    local = now.astimezone(KST)
    return local.weekday() == 6 and local.time() >= dt.time(9, 0)


def request_json(request: Request, service: str) -> dict:
    try:
        with urlopen(request, timeout=20) as response:
            value = json.load(response)
    except HTTPError as err:
        raise StudylogError(f"{service} HTTP {err.code}.") from None
    except (URLError, TimeoutError, OSError):
        raise StudylogError(f"{service} connection failed.") from None
    except (ValueError, TypeError):
        raise StudylogError(f"{service} returned invalid JSON.") from None
    if not isinstance(value, dict):
        raise StudylogError(f"{service} returned invalid data.")
    return value


class BandWriter:
    """Only checks write permission and creates posts on one specified band."""

    def __init__(self, access_token: str, band_key: str):
        self.access_token = access_token
        self.band_key = band_key

    def call(self, method: str, path: str, fields: dict) -> dict:
        parameters = {
            "access_token": self.access_token,
            "band_key": self.band_key,
            **fields,
        }
        encoded = urlencode(parameters).encode("utf-8")
        if method == "POST":
            request = Request(BAND_URL + path, data=encoded, method="POST")
        else:
            request = Request(BAND_URL + path + "?" + encoded.decode("ascii"), method="GET")
        response = request_json(request, "BAND API")
        if response.get("result_code") != 1:
            raise StudylogError("BAND API returned a non-success result.")
        return response.get("result_data") or {}

    def require_posting_permission(self) -> None:
        data = self.call("GET", "/v2/band/permissions", {"permissions": "posting"})
        allowed = data.get("permissions", data.get("permission", []))
        if "posting" not in (allowed or []):
            raise StudylogError("Posting permission has not been granted for this band.")

    def create_post(self, text: str) -> None:
        data = self.call(
            "POST", "/v2.2/band/post/create", {"content": text, "do_push": "false"}
        )
        if not data.get("post_key"):
            raise StudylogError("BAND response has no post_key; manual confirmation required.")


class GitHubLedger:
    """Date/status flags only. Never stores member data or BAND credentials."""

    def __init__(self, token: str, repo: str, branch: str = "main"):
        if repo.count("/") != 1:
            raise StudylogError("Invalid GITHUB_REPOSITORY.")
        self.url_prefix = f"{GITHUB_URL}/repos/{repo}/contents/.studylog-state"
        self.branch = branch
        self.headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.sha: str | None = None

    def load(self, sunday: dt.date) -> dict[str, str]:
        url = f"{self.url_prefix}/{sunday.isoformat()}.json"
        request = Request(url + "?ref=" + quote(self.branch), headers=self.headers)
        try:
            with urlopen(request, timeout=20) as response:
                data = json.load(response)
        except HTTPError as err:
            if err.code == 404:
                self.sha = None
                return {}
            raise StudylogError(f"GitHub state read failed: HTTP {err.code}.") from None
        except (URLError, TimeoutError, OSError):
            raise StudylogError("GitHub state read connection failed.") from None
        try:
            self.sha = data["sha"]
            state = json.loads(base64.b64decode(data["content"]).decode("utf-8"))
            if not isinstance(state, dict) or any(
                key not in weekly_posts(sunday) or status not in ("attempted", "posted")
                for key, status in state.items()
            ):
                raise ValueError("Invalid state.")
            return state
        except (ValueError, TypeError, KeyError):
            raise StudylogError("GitHub state is invalid.") from None

    def save(self, sunday: dt.date, state: dict[str, str]) -> None:
        url = f"{self.url_prefix}/{sunday.isoformat()}.json"
        content = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        payload = {
            "message": f"Studylog posting status for {sunday.isoformat()}",
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if self.sha:
            payload["sha"] = self.sha
        req = Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={**self.headers, "Content-Type": "application/json"}, method="PUT"
        )
        data = request_json(req, "GitHub state update")
        try:
            self.sha = data["content"]["sha"]
        except (KeyError, TypeError):
            raise StudylogError("GitHub did not confirm the state update.") from None


def publish(sunday: dt.date, writer: BandWriter, ledger: GitHubLedger) -> tuple[int, int]:
    writer.require_posting_permission()
    state = ledger.load(sunday)
    posted = skipped = 0
    for title in weekly_posts(sunday):
        if title in state:
            print(f"Skip {title} ({state[title]}).")
            skipped += 1
            continue
        # Mark BEFORE submitting. Failed/uncertain requests are never auto-retried.
        state[title] = "attempted"
        ledger.save(sunday, state)
        writer.create_post(title)
        state[title] = "posted"
        ledger.save(sunday, state)
        print(f"Created {title}.")
        posted += 1
    return posted, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly BAND post writer")
    parser.add_argument("--mode", choices=("preview", "publish"), default="preview")
    args = parser.parse_args(argv)
    now = dt.datetime.now(KST)
    sunday = sunday_for(now.date())
    if args.mode == "preview":
        print(f"Scheduled Sunday: {sunday} 09:00 KST")
        print("\n".join(weekly_posts(sunday)))
        print("Preview only. No BAND access.")
        return 0

    if not scheduled_now(now):
        print("Not Sunday at/after 09:00 KST. Skipping.")
        return 0
    if os.getenv("BAND_LIVE_ENABLED", "").lower() != "true":
        print("BAND_LIVE_ENABLED not true. Posting disabled.")
        return 0
    token = os.getenv("BAND_ACCESS_TOKEN")
    band = os.getenv("BAND_KEY")
    github_token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not all((token, band, github_token, repo)):
        raise StudylogError("Missing BAND credentials or GitHub posting-state credentials.")
    counts = publish(
        sunday, BandWriter(token, band),
        GitHubLedger(github_token, repo, os.getenv("GITHUB_REF_NAME") or "main")
    )
    print(f"Finished. Created: {counts[0]}, skipped: {counts[1]}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StudylogError as exc:
        print("Studylog error:", exc, file=sys.stderr)
        sys.exit(1)
