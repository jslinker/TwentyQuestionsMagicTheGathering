#!/usr/bin/env python3
"""Download a one-off snapshot of Scryfall Tagger's Oracle tags.

The Tagger bulk endpoint is useful but undocumented. This script intentionally
runs only when invoked, writes a dated local snapshot, and never makes the game
or decision-tree generator depend on a live network request.
"""

import argparse
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ORACLE_TAGS_URL = "https://api.scryfall.com/private/tags/oracle"
DEFAULT_USER_AGENT = "MagicGuessingGame-OracleTagSnapshot/1.0"


def parse_args():
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    parser = argparse.ArgumentParser(
        description=(
            "Download and validate a timestamped snapshot of Scryfall Tagger "
            "Oracle tags for reproducible local tree generation."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "snapshots" / f"oracle-tags-snapshot-{timestamp}.json",
        help="output path (default: a timestamped file in data/snapshots/)",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="descriptive HTTP User-Agent sent to Scryfall",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="request timeout in seconds (default: 60)",
    )
    parser.add_argument(
        "--ca-file",
        type=Path,
        help="CA certificate bundle to use instead of automatic detection",
    )
    parser.add_argument(
        "--transport",
        choices=("auto", "curl", "urllib"),
        default="curl",
        help="HTTP transport (default: curl; avoids common Python certificate-store issues)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace --output if it already exists",
    )
    return parser.parse_args()


def validate_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("expected a top-level JSON object")
    tags = payload.get("data")
    if not isinstance(tags, list) or not tags:
        raise ValueError("expected a non-empty top-level 'data' array")

    label_counts = Counter()
    unique_oracle_ids = set()
    assignment_count = 0
    malformed = []

    for index, tag in enumerate(tags):
        if not isinstance(tag, dict):
            malformed.append(f"data[{index}] is not an object")
            continue
        label = tag.get("label")
        oracle_ids = tag.get("oracle_ids")
        if not isinstance(label, str) or not label.strip():
            malformed.append(f"data[{index}] has no non-empty label")
            continue
        if not isinstance(oracle_ids, list) or any(not isinstance(value, str) for value in oracle_ids):
            malformed.append(f"tag {label!r} has an invalid oracle_ids array")
            continue
        label_counts[label] += 1
        unique_oracle_ids.update(oracle_ids)
        assignment_count += len(oracle_ids)

    if malformed:
        preview = "; ".join(malformed[:5])
        raise ValueError(f"malformed tag data ({len(malformed)} problems): {preview}")
    duplicated_labels = sorted(label for label, count in label_counts.items() if count > 1)

    return {
        "tag_count": len(tags),
        "unique_label_count": len(label_counts),
        "duplicate_label_row_count": sum(count - 1 for count in label_counts.values()),
        "duplicated_labels": duplicated_labels,
        "assignment_count": assignment_count,
        "unique_oracle_id_count": len(unique_oracle_ids),
    }


def make_ssl_context(ca_file=None):
    if ca_file is not None:
        resolved = ca_file.expanduser().resolve()
        if not resolved.is_file():
            raise ValueError(f"CA certificate bundle does not exist: {resolved}")
        return ssl.create_default_context(cafile=resolved), resolved

    # python.org macOS installations may not configure OpenSSL's default CA
    # path until Install Certificates.command has been run. Prefer certifi when
    # it is already installed, then try standard macOS/Homebrew bundles.
    # Prefer machine-managed bundles because they may contain a local/network
    # CA that the public certifi bundle intentionally does not include.
    candidates = [
        Path("/etc/ssl/cert.pem"),
        Path("/opt/homebrew/etc/ca-certificates/cert.pem"),
        Path("/usr/local/etc/openssl@3/cert.pem"),
    ]
    try:
        import certifi
        candidates.append(Path(certifi.where()))
    except ImportError:
        pass
    for candidate in candidates:
        if candidate.is_file():
            return ssl.create_default_context(cafile=candidate), candidate
    return ssl.create_default_context(), None


def download_with_urllib(url, user_agent, timeout, ca_file=None, attempts=3):
    headers = {
        "Accept": "application/json;q=0.9,*/*;q=0.8",
        "User-Agent": user_agent,
    }
    request = urllib.request.Request(url, headers=headers)
    ssl_context, selected_ca_file = make_ssl_context(ca_file)
    if selected_ca_file:
        print(f"Using CA certificates: {selected_ca_file}")

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            if not retryable or attempt == attempts:
                if error.code in {401, 403, 404}:
                    raise RuntimeError(f"Scryfall returned HTTP {error.code} for {request.full_url}") from error
                raise RuntimeError(f"Scryfall returned HTTP {error.code}: {error.reason}") from error
            retry_after = error.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else attempt * 2.0
            print(f"Request returned HTTP {error.code}; retrying in {delay:g}s...", file=sys.stderr)
            time.sleep(delay)
        except urllib.error.URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                raise RuntimeError(f"Python SSL verification failed: {error.reason}") from error
            if attempt == attempts:
                raise RuntimeError(f"could not reach Scryfall: {error.reason}") from error
            delay = attempt * 2.0
            print(f"Network error; retrying in {delay:g}s...", file=sys.stderr)
            time.sleep(delay)

    raise RuntimeError("download failed")


def download_with_curl(url, user_agent, timeout):
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("curl transport requested, but curl is not installed")
    command = [
        curl,
        "--fail",
        "--location",
        "--silent",
        "--show-error",
        "--retry", "2",
        "--retry-delay", "2",
        "--connect-timeout", str(max(1, int(timeout))),
        "--max-time", str(max(1, int(timeout))),
        "--header", "Accept: application/json;q=0.9,*/*;q=0.8",
        "--user-agent", user_agent,
        url,
    ]
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"curl failed with exit code {result.returncode}: {message}")
    if not result.stdout:
        raise RuntimeError("curl returned an empty response")
    return result.stdout


def download(url, user_agent, timeout, ca_file=None, transport="auto"):
    if transport == "curl":
        print("Downloading with curl...")
        return download_with_curl(url, user_agent, timeout)
    try:
        return download_with_urllib(url, user_agent, timeout, ca_file=ca_file)
    except RuntimeError as error:
        if transport != "auto":
            raise
        print(f"Python download failed: {error}", file=sys.stderr)
        print("Falling back to curl...", file=sys.stderr)
        return download_with_curl(url, user_agent, timeout)


def write_snapshot(output_path, raw_payload, payload, statistics, force):
    output_path = output_path.resolve()
    if output_path.exists() and not force:
        raise FileExistsError(f"{output_path} already exists; use --force to replace it")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    snapshot = dict(payload)
    snapshot["_snapshot_metadata"] = {
        "source": ORACLE_TAGS_URL,
        "fetched_at": fetched_at,
        "response_sha256": hashlib.sha256(raw_payload).hexdigest(),
        **statistics,
    }

    temporary_path = output_path.with_name(f".{output_path.name}.{os.getpid()}.tmp")
    try:
        with temporary_path.open("x", encoding="utf-8") as file:
            json.dump(snapshot, file, separators=(",", ":"), ensure_ascii=False)
            file.write("\n")
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return output_path, fetched_at


def main():
    args = parse_args()
    try:
        raw_payload = download(
            ORACLE_TAGS_URL,
            user_agent=args.user_agent,
            timeout=args.timeout,
            ca_file=args.ca_file,
            transport=args.transport,
        )
        payload = json.loads(raw_payload)
        statistics = validate_payload(payload)
        output_path, fetched_at = write_snapshot(
            args.output,
            raw_payload,
            payload,
            statistics,
            args.force,
        )
    except (FileExistsError, json.JSONDecodeError, RuntimeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(f"Saved Oracle tag snapshot: {output_path}")
    print(f"Fetched: {fetched_at}")
    print(f"Tags: {statistics['tag_count']:,}")
    print(f"Unique labels: {statistics['unique_label_count']:,}")
    if statistics["duplicate_label_row_count"]:
        print(
            "Duplicate-label rows preserved: "
            f"{statistics['duplicate_label_row_count']:,} "
            f"({', '.join(statistics['duplicated_labels'])})"
        )
    print(f"Tag-to-card assignments: {statistics['assignment_count']:,}")
    print(f"Unique Oracle IDs tagged: {statistics['unique_oracle_id_count']:,}")
    print(f"Build with: python3 scripts/pipeline.py build --snapshot {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
