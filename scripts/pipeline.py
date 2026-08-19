#!/usr/bin/env python3
"""Reproducible download, build, verification, and preview pipeline."""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from generate_tree import normal_deck_card_exclusion


ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
SCRIPTS_DIR = ROOT / "scripts"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
BUILD_DIR = ROOT / "build"


def run(command):
    display = " ".join(shlex.quote(str(part)) for part in command)
    print(f"+ {display}", flush=True)
    subprocess.run([str(part) for part in command], cwd=ROOT, check=True)


def timestamp():
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def latest_tag_snapshot():
    candidates = list(SNAPSHOT_DIR.glob("oracle-tags-snapshot-*.json"))
    candidates.extend(ROOT.glob("oracle-tags-snapshot-*.json"))
    if not candidates:
        raise FileNotFoundError(
            "No Oracle-tag snapshot found. Run: python3 scripts/pipeline.py refresh"
        )
    return max(candidates, key=lambda path: (path.name, path.stat().st_mtime_ns))


def refresh(transport, tags_only=False):
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    if not tags_only:
        run([
            sys.executable, SCRIPTS_DIR / "fetch_card_data.py",
            "--transport", transport,
            "--output", DATA_DIR / "card-data.json",
            "--metadata-output", DATA_DIR / "card-data.metadata.json",
        ])
    snapshot = SNAPSHOT_DIR / f"oracle-tags-snapshot-{timestamp()}.json"
    run([
        sys.executable, SCRIPTS_DIR / "fetch_oracle_tags.py",
        "--transport", transport,
        "--output", snapshot,
    ])
    return snapshot


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compact_card(card):
    compact = {
        "name": card["name"],
        "layout": card.get("layout"),
        "type_line": card.get("type_line", ""),
    }
    if card.get("image_uris", {}).get("normal"):
        compact["image_uris"] = {"normal": card["image_uris"]["normal"]}
    faces = []
    for face in card.get("card_faces", []):
        compact_face = {"type_line": face.get("type_line", "")}
        if face.get("image_uris", {}).get("normal"):
            compact_face["image_uris"] = {"normal": face["image_uris"]["normal"]}
        faces.append(compact_face)
    if faces:
        compact["card_faces"] = faces
    return compact


def write_json(path, value, *, indent=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":"))
            file.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def package_site(snapshot):
    tree_path = BUILD_DIR / "decision-tree.json"
    metrics_path = BUILD_DIR / "build-metrics.json"
    with (DATA_DIR / "card-data.json").open("r", encoding="utf-8") as file:
        cards = json.load(file)
    normal_cards = [compact_card(card) for card in cards if normal_deck_card_exclusion(card) is None]

    shutil.copy2(tree_path, ROOT / "decision-tree.json")
    shutil.copy2(metrics_path, ROOT / "build-metrics.json")
    write_json(ROOT / "card-data.json", normal_cards)
    (ROOT / ".nojekyll").touch()

    with metrics_path.open("r", encoding="utf-8") as file:
        metrics = json.load(file)
    build_info = {
        "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tag_snapshot": snapshot.name,
        "inputs": {
            "card_data_sha256": sha256_file(DATA_DIR / "card-data.json"),
            "oracle_tags_sha256": sha256_file(snapshot),
            "semantic_questions_sha256": sha256_file(CONFIG_DIR / "semantic-questions.json"),
            "generator_sha256": sha256_file(SCRIPTS_DIR / "generate_tree.py"),
        },
        "tree_metrics": metrics,
    }
    write_json(ROOT / "build-info.json", build_info, indent=2)

    verify_site()
    print(f"Packaged GitHub Pages site at repository root: {ROOT}")


def verify_site():
    required = [
        "index.html", "decision-tree.json", "card-data.json",
        "build-metrics.json", "build-info.json", ".nojekyll",
    ]
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        raise RuntimeError(f"incomplete site bundle: {', '.join(missing)}")

    html = (ROOT / "index.html").read_text(encoding="utf-8")
    for asset in ("decision-tree.json", "card-data.json"):
        if f"fetch('{asset}')" not in html and f'fetch("{asset}")' not in html:
            raise ValueError(f"index.html does not fetch its packaged {asset}")
    if "name-phase-notice" not in html or "currentNode.phase === 'name'" not in html:
        raise ValueError("index.html does not expose the generated name-tiebreaker phase")

    with (ROOT / "decision-tree.json").open("r", encoding="utf-8") as file:
        tree = json.load(file)
    with (ROOT / "card-data.json").open("r", encoding="utf-8") as file:
        cards = json.load(file)
    with (ROOT / "build-metrics.json").open("r", encoding="utf-8") as file:
        metrics = json.load(file)
    with (ROOT / "build-info.json").open("r", encoding="utf-8") as file:
        build_info = json.load(file)

    if not isinstance(cards, list) or not cards:
        raise ValueError("packaged card-data.json is not a non-empty array")
    excluded = [card["name"] for card in cards if normal_deck_card_exclusion(card) is not None]
    if excluded:
        raise ValueError(f"packaged card data contains excluded game pieces: {excluded[:5]}")

    card_names = {card["name"] for card in cards}
    image_names = {
        card["name"] for card in cards
        if card.get("image_uris", {}).get("normal")
        or any(face.get("image_uris", {}).get("normal") for face in card.get("card_faces", []))
    }
    represented_names = set()
    represented_records = 0
    decision_nodes = 0
    name_question_nodes = 0
    ambiguous_leaf_count = 0

    def walk(node):
        nonlocal represented_records, decision_nodes, name_question_nodes, ambiguous_leaf_count
        if not isinstance(node, dict):
            raise ValueError("decision tree contains a non-object node")
        if "card_name" in node:
            names = [node["card_name"]]
            record_count = node.get("record_count", 1)
        elif "remainingPossibleCardNames" in node:
            names = node["remainingPossibleCardNames"]
            if not isinstance(names, list) or not names:
                raise ValueError("decision tree contains an empty ambiguous leaf")
            ambiguous_leaf_count += 1
        else:
            if not isinstance(node.get("question"), str) or "yes" not in node or "no" not in node:
                raise ValueError("decision tree contains a malformed decision node")
            if node.get("phase") == "name":
                if not isinstance(node.get("name_test"), dict) or not isinstance(node.get("fallback_group_size"), int):
                    raise ValueError("name-tiebreaker node is missing its generated test metadata")
                name_question_nodes += 1
            decision_nodes += 1
            walk(node["yes"])
            walk(node["no"])
            return
        if any(not isinstance(name, str) or not name for name in names):
            raise ValueError("decision tree contains an invalid card name")
        represented_names.update(names)
        represented_records += record_count if "card_name" in node else len(names)

    walk(tree)
    missing_cards = sorted(represented_names - card_names)
    missing_images = sorted(represented_names - image_names)
    if missing_cards:
        raise ValueError(f"tree cards missing from browser lookup: {missing_cards[:5]}")
    if missing_images:
        raise ValueError(f"tree cards missing a normal image URL: {missing_images[:5]}")
    if represented_records != metrics.get("represented_records"):
        raise ValueError("tree record count does not match build-metrics.json")
    if decision_nodes != metrics.get("decision_nodes"):
        raise ValueError("tree decision count does not match build-metrics.json")
    if build_info.get("tree_metrics", {}).get("represented_records") != represented_records:
        raise ValueError("build-info.json does not describe the packaged tree")
    if ambiguous_leaf_count:
        raise ValueError(f"packaged tree still contains {ambiguous_leaf_count} unresolved leaves")
    expected_name_nodes = metrics.get("name_fallback", {}).get("name_question_nodes")
    if name_question_nodes != expected_name_nodes:
        raise ValueError("name-tiebreaker node count does not match build-metrics.json")

    print(
        f"Verified browser bundle: {represented_records:,} records, "
        f"{len(represented_names):,} card names, {decision_nodes:,} decision nodes, "
        f"{name_question_nodes:,} name tiebreakers, 100% image lookup coverage."
    )


def build(snapshot=None):
    snapshot = (snapshot or latest_tag_snapshot()).resolve()
    if not snapshot.is_file():
        raise FileNotFoundError(f"Oracle-tag snapshot does not exist: {snapshot}")
    if not (DATA_DIR / "card-data.json").is_file():
        raise FileNotFoundError(
            "data/card-data.json is missing. Run: python3 scripts/pipeline.py refresh"
        )
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    run([
        sys.executable, SCRIPTS_DIR / "generate_tree.py",
        "--card-data", DATA_DIR / "card-data.json",
        "--oracle-tags", snapshot,
        "--semantic-config", CONFIG_DIR / "semantic-questions.json",
        "--output", BUILD_DIR / "decision-tree.json",
        "--metrics-output", BUILD_DIR / "build-metrics.json",
    ])
    package_site(snapshot)
    return snapshot


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    refresh_parser = subparsers.add_parser("refresh", help="download both datasets, build, and package")
    refresh_parser.add_argument("--transport", choices=("curl", "auto", "urllib"), default="curl")

    tags_parser = subparsers.add_parser("refresh-tags", help="download tags, build, and package")
    tags_parser.add_argument("--transport", choices=("curl", "auto", "urllib"), default="curl")

    build_parser = subparsers.add_parser("build", help="build and package from local inputs")
    build_parser.add_argument("--snapshot", type=Path)

    serve_parser = subparsers.add_parser("serve", help="serve the packaged site locally")
    serve_parser.add_argument("--port", type=int, default=8000)

    subparsers.add_parser("verify", help="verify the exact static browser bundle in dist")
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        if args.action == "refresh":
            build(refresh(args.transport))
        elif args.action == "refresh-tags":
            build(refresh(args.transport, tags_only=True))
        elif args.action == "build":
            build(args.snapshot)
        elif args.action == "serve":
            if not all(
                (ROOT / name).is_file()
                for name in ("index.html", "decision-tree.json", "card-data.json")
            ):
                build()
            run([sys.executable, "-m", "http.server", str(args.port), "--directory", ROOT])
        elif args.action == "verify":
            verify_site()
    except (FileNotFoundError, json.JSONDecodeError, OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
