#!/usr/bin/env python3
"""Rank unused Oracle tags by how well they split the current ambiguous leaves."""

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from generate_tree import load_semantic_questions, make_questions, normal_deck_card_exclusion

ROOT = Path(__file__).resolve().parent.parent


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card-data", type=Path, default=ROOT / "data" / "card-data.json")
    parser.add_argument("--tree", type=Path, default=ROOT / "build" / "decision-tree.json")
    parser.add_argument("--oracle-tags", type=Path, required=True)
    parser.add_argument(
        "--semantic-config", type=Path, default=ROOT / "config" / "semantic-questions.json"
    )
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "tag-opportunities.json")
    parser.add_argument("--limit", type=int, default=250)
    return parser.parse_args()


def main():
    args = parse_args()
    with args.card_data.open("r", encoding="utf-8") as file:
        cards = [card for card in json.load(file) if normal_deck_card_exclusion(card) is None]
    with args.tree.open("r", encoding="utf-8") as file:
        tree = json.load(file)
    with args.oracle_tags.open("r", encoding="utf-8") as file:
        tag_snapshot = json.load(file)
    with args.semantic_config.open("r", encoding="utf-8") as file:
        configured = json.load(file)

    semantic_questions, _ = load_semantic_questions(args.oracle_tags, args.semantic_config)
    questions = make_questions(cards, semantic_questions)
    predicates = {text: function for function, text in questions}

    leaf_cards = defaultdict(list)
    leaf_for_oracle_id = {}
    for card in cards:
        node = tree
        while "question" in node and node.get("phase") != "name":
            node = node["yes"] if predicates[node["question"]](card) else node["no"]
        leaf_id = id(node)
        leaf_cards[leaf_id].append(card)
        leaf_for_oracle_id[card["oracle_id"]] = leaf_id

    leaf_sizes = {leaf_id: len(group) for leaf_id, group in leaf_cards.items()}
    ambiguous_ids = {leaf_id for leaf_id, size in leaf_sizes.items() if size > 1}
    configured_tags = {entry["tag"] for entry in configured["questions"]}

    oracle_ids_by_tag = defaultdict(set)
    for row in tag_snapshot["data"]:
        oracle_ids_by_tag[row["label"]].update(row["oracle_ids"])

    rankings = []
    for label, tagged_oracle_ids in oracle_ids_by_tag.items():
        if label in configured_tags:
            continue
        counts = Counter(
            leaf_for_oracle_id[oracle_id]
            for oracle_id in tagged_oracle_ids
            if oracle_id in leaf_for_oracle_id and leaf_for_oracle_id[oracle_id] in ambiguous_ids
        )
        gini_gain = 0.0
        entropy_bits = 0.0
        separated_pairs = 0
        split_leaves = 0
        singleton_opportunities = 0
        affected_records = 0
        for leaf_id, yes_count in counts.items():
            size = leaf_sizes[leaf_id]
            no_count = size - yes_count
            if not yes_count or not no_count:
                continue
            split_leaves += 1
            affected_records += size
            separated_pairs += yes_count * no_count
            gini_gain += 2 * yes_count * no_count / size
            probability = yes_count / size
            entropy_bits += size * (
                -probability * math.log2(probability)
                -(1 - probability) * math.log2(1 - probability)
            )
            singleton_opportunities += int(yes_count == 1) + int(no_count == 1)
        if split_leaves:
            included_matches = sum(oracle_id in leaf_for_oracle_id for oracle_id in tagged_oracle_ids)
            rankings.append({
                "tag": label,
                "included_matches": included_matches,
                "split_leaves": split_leaves,
                "affected_ambiguous_records": affected_records,
                "singleton_opportunities": singleton_opportunities,
                "separated_pairs": separated_pairs,
                "gini_gain": round(gini_gain, 4),
                "entropy_bits": round(entropy_bits, 4),
            })

    rankings.sort(key=lambda row: (row["gini_gain"], row["separated_pairs"]), reverse=True)
    report = {
        "ambiguous_records": sum(leaf_sizes[leaf_id] for leaf_id in ambiguous_ids),
        "ambiguous_leaves": len(ambiguous_ids),
        "unused_tags_evaluated": len(rankings),
        "ranking_note": "Scores estimate one-question splits of the current leaves; they are not claims about player-facing clarity.",
        "tags": rankings[:args.limit],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as file:
        json.dump(report, file, indent=2)
        file.write("\n")
    print(f"Ranked {len(rankings):,} unused tags against {report['ambiguous_records']:,} ambiguous records.")
    print(f"Saved the top {min(args.limit, len(rankings)):,}: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
