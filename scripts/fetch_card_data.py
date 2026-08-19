#!/usr/bin/env python3
"""Download and validate Scryfall's current Oracle Cards bulk dataset."""

import argparse
import gzip
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fetch_oracle_tags import download


ROOT = Path(__file__).resolve().parent.parent
BULK_DATA_URL = "https://api.scryfall.com/bulk-data"
BULK_DATA_TYPE = "oracle_cards"
BULK_DATA_SLUG = "oracle-cards"
BULK_DATA_ITEM_URL = f"{BULK_DATA_URL}/{BULK_DATA_SLUG}"
DEFAULT_USER_AGENT = "MagicGuessingGame-CardSnapshot/1.0"


def parse_args():
    parser = argparse.ArgumentParser(description="Download Scryfall's Oracle Cards bulk dataset.")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "card-data.json")
    parser.add_argument(
        "--metadata-output", type=Path, default=ROOT / "data" / "card-data.metadata.json"
    )
    parser.add_argument("--transport", choices=("auto", "curl", "urllib"), default="curl")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--ca-file", type=Path)
    return parser.parse_args()


def atomic_write(path, data):
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as file:
            file.write(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def select_oracle_cards_download(metadata):
    rows = metadata.get("data") if isinstance(metadata, dict) else None
    if not isinstance(rows, list):
        raise ValueError("bulk-data response has no 'data' array")
    matches = [row for row in rows if isinstance(row, dict) and row.get("type") == BULK_DATA_TYPE]
    if len(matches) != 1:
        raise ValueError(f"expected one Oracle Cards bulk-data entry; found {len(matches)}")
    selected = matches[0]
    content_type = selected.get("content_type")
    if content_type is not None and (
        not isinstance(content_type, str) or "json" not in content_type.casefold()
    ):
        raise ValueError(f"Oracle Cards bulk-data entry is not JSON: {content_type}")
    return selected


def normalize_download_uri(value):
    """Return an HTTPS download URI, accepting Scryfall's relative/HTTP variants."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.startswith("//"):
        value = f"https:{value}"
    parsed = urlparse(value)
    if parsed.scheme == "http" and parsed.netloc:
        value = parsed._replace(scheme="https").geturl()
        parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.netloc:
        return value
    return None


def select_bulk_file(entry):
    """Choose Scryfall's array JSON or newer JSON Lines bulk representation."""
    for field, payload_format in (
        ("download_uri", "json-array"),
        ("jsonl_download_uri", "json-lines"),
    ):
        uri = normalize_download_uri(entry.get(field))
        if uri is not None:
            return uri, payload_format, field
    return None, None, None


def select_oracle_cards_detail(metadata):
    if not isinstance(metadata, dict) or metadata.get("type") != BULK_DATA_TYPE:
        received = metadata.get("type") if isinstance(metadata, dict) else type(metadata).__name__
        raise ValueError(f"Oracle Cards detail endpoint returned the wrong payload type: {received}")
    return metadata


def parse_card_payload(raw_payload, format_hint=None):
    compression = "none"
    decoded_payload = raw_payload
    if raw_payload.startswith(b"\x1f\x8b"):
        decoded_payload = gzip.decompress(raw_payload)
        compression = "gzip"

    text = decoded_payload.decode("utf-8")
    stripped = text.lstrip()
    if not stripped:
        raise ValueError("Oracle Cards download is empty after decompression")

    if stripped.startswith("["):
        cards = json.loads(text)
        detected_format = "json-array"
    else:
        cards = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                cards.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSON Lines record at line {line_number}: {error}") from error
        detected_format = "json-lines"

    if format_hint and detected_format != format_hint:
        print(
            f"Download was advertised as {format_hint} but decoded as {detected_format}; "
            "continuing after structural validation."
        )
    return cards, detected_format, compression


def validate_cards(payload):
    if not isinstance(payload, list) or not payload:
        raise ValueError("Oracle Cards download is not a non-empty array")
    malformed = []
    oracle_ids = set()
    layouts = Counter()
    for index, card in enumerate(payload):
        if not isinstance(card, dict):
            malformed.append(f"record {index} is not an object")
            continue
        if not isinstance(card.get("name"), str) or not isinstance(card.get("oracle_id"), str):
            malformed.append(f"record {index} is missing name or oracle_id")
            continue
        oracle_ids.add(card["oracle_id"])
        layouts[card.get("layout", "unknown")] += 1
    if malformed:
        raise ValueError(f"malformed Oracle Cards data: {'; '.join(malformed[:5])}")
    if len(oracle_ids) != len(payload):
        raise ValueError("Oracle Cards dataset contains duplicate oracle_id values")
    return {"record_count": len(payload), "unique_oracle_id_count": len(oracle_ids), "layouts": dict(layouts)}


def main():
    args = parse_args()
    try:
        print("Fetching Scryfall bulk-data metadata...")
        metadata_raw = download(
            BULK_DATA_URL, args.user_agent, args.timeout,
            ca_file=args.ca_file, transport=args.transport,
        )
        bulk_entry = select_oracle_cards_download(json.loads(metadata_raw))
        download_uri, payload_format, download_field = select_bulk_file(bulk_entry)
        if download_uri is None:
            # Some catalog responses omit the file URI. Ask the type-specific
            # metadata endpoint before falling back to its documented file mode.
            detail_uri = normalize_download_uri(bulk_entry.get("uri")) or BULK_DATA_ITEM_URL
            print(f"Catalog entry has no usable download_uri; fetching {BULK_DATA_TYPE} details...")
            detail_raw = download(
                detail_uri, args.user_agent, args.timeout,
                ca_file=args.ca_file, transport=args.transport,
            )
            detail_entry = select_oracle_cards_detail(json.loads(detail_raw))
            bulk_entry = {**bulk_entry, **detail_entry}
            download_uri, payload_format, download_field = select_bulk_file(bulk_entry)
        if download_uri is None:
            download_uri = f"{BULK_DATA_ITEM_URL}?format=file"
            payload_format = None
            download_field = "type-specific-file-endpoint"
            available_fields = ", ".join(sorted(bulk_entry))
            print(
                "Oracle Cards metadata still has no usable download_uri or jsonl_download_uri; "
                f"using the type-specific file endpoint. Fields received: {available_fields}"
            )
        print("Downloading Oracle Cards bulk data...")
        cards_raw = download(
            download_uri, args.user_agent, args.timeout,
            ca_file=args.ca_file, transport=args.transport,
        )
        cards, detected_format, compression = parse_card_payload(cards_raw, payload_format)
        statistics = validate_cards(cards)
        normalized_cards = (json.dumps(cards, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        output = atomic_write(args.output, normalized_cards)
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        metadata = {
            "source": BULK_DATA_URL,
            "bulk_data_type": BULK_DATA_TYPE,
            "bulk_data_name": bulk_entry.get("name"),
            "bulk_data_id": bulk_entry.get("id"),
            "download_uri": download_uri,
            "download_uri_field": download_field,
            "download_payload_format": detected_format,
            "download_compression": compression,
            "content_type": bulk_entry.get("content_type"),
            "content_encoding": bulk_entry.get("content_encoding"),
            "scryfall_updated_at": bulk_entry.get("updated_at"),
            "fetched_at": fetched_at,
            "response_sha256": hashlib.sha256(cards_raw).hexdigest(),
            "normalized_output_sha256": hashlib.sha256(normalized_cards).hexdigest(),
            **statistics,
        }
        metadata_output = atomic_write(
            args.metadata_output,
            (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
    except (json.JSONDecodeError, OSError, RuntimeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(f"Saved {statistics['record_count']:,} Oracle card records: {output}")
    print(f"Saved download metadata: {metadata_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
