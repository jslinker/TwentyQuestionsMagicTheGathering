import argparse
import json
import math
import re
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CARD_DATA_FILE = ROOT / "data" / "card-data.json"
DEFAULT_TREE_FILE = ROOT / "build" / "decision-tree.json"
DEFAULT_SEMANTIC_CONFIG_FILE = ROOT / "config" / "semantic-questions.json"
DEFAULT_METRICS_FILE = ROOT / "build" / "build-metrics.json"


def type_parts(card):
    """Return normalized type/supertype and subtype words from every face."""
    lines = [card.get("type_line", "")]
    lines.extend(face.get("type_line", "") for face in card.get("card_faces", []))
    left_words, subtype_words = set(), set()
    for line in lines:
        for face_line in line.split(" // "):
            left, separator, right = face_line.partition(" — ")
            left_words.update(left.split())
            if separator:
                subtype_words.update(right.split())
    return left_words, subtype_words


def card_colors(card):
    colors = card.get("colors")
    if colors is not None:
        return set(colors)
    return {color for face in card.get("card_faces", []) for color in face.get("colors", [])}


def mana_costs(card):
    costs = [card["mana_cost"]] if card.get("mana_cost") else []
    costs.extend(face["mana_cost"] for face in card.get("card_faces", []) if face.get("mana_cost"))
    return costs


def oracle_text(card):
    """Return the visible Oracle rules text from the card and all of its faces."""
    texts = [card["oracle_text"]] if card.get("oracle_text") else []
    texts.extend(face["oracle_text"] for face in card.get("card_faces", []) if face.get("oracle_text"))
    return "\n".join(texts)


def primary_creature_face(card):
    """Return the card/front creature face used for printed P/T questions."""
    if "Creature" in type_parts(card)[0] and (card.get("power") is not None or card.get("toughness") is not None):
        return card
    for face in card.get("card_faces", []):
        if "Creature" in type_parts(face)[0] and (face.get("power") is not None or face.get("toughness") is not None):
            return face
    return None


def numeric_value(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def article(word):
    return "an" if word[:1].lower() in "aeiou" else "a"


def normal_deck_card_exclusion(card):
    """Return why a record is not a normal main/sideboard deck card, or None."""
    excluded_layouts = {
        "art_series",
        "double_faced_token",
        "emblem",
        "planar",
        "scheme",
        "token",
        "vanguard",
    }
    if card.get("layout") in excluded_layouts:
        return f"layout:{card.get('layout')}"

    left_words, subtype_words = type_parts(card)
    normalized_left = {word.lower() for word in left_words}
    normalized_subtypes = {word.lower() for word in subtype_words}
    excluded_types = {
        "card",          # art cards, checklists, and minigame/memorabilia cards
        "conspiracy",   # starts outside the deck
        "dungeon",      # command-zone game piece
        "emblem",
        "hero",         # Hero's Path external game piece
        "phenomenon",
        "plane",
        "scheme",
        "stickers",
        "token",
        "vanguard",
    }
    excluded_subtypes = {
        "attraction",   # kept in a separate Attraction deck
        "contraption",  # kept in a separate Contraption deck
    }
    if normalized_left & excluded_types:
        return f"type:{sorted(normalized_left & excluded_types)[0]}"
    if normalized_subtypes & excluded_subtypes:
        return f"subtype:{sorted(normalized_subtypes & excluded_subtypes)[0]}"
    return None


def make_questions(cards, semantic_questions=()):
    questions = []

    # Color identity and the card's actual colors are deliberately separate.
    for symbol, name in {"W": "white", "U": "blue", "B": "black", "R": "red", "G": "green"}.items():
        questions.append((lambda card, symbol=symbol: symbol in card.get("color_identity", []), f"Does its color identity include {name}?"))
    questions.extend([
        (lambda card: len(card.get("color_identity", [])) == 0, "Is its color identity colorless?"),
        (lambda card: len(card.get("color_identity", [])) == 1, "Does its color identity contain exactly one color?"),
        (lambda card: len(card.get("color_identity", [])) >= 2, "Is its color identity multicolored?"),
        (lambda card: len(card.get("color_identity", [])) == 2, "Does its color identity contain exactly two colors?"),
        (lambda card: len(card.get("color_identity", [])) == 3, "Does its color identity contain exactly three colors?"),
        (lambda card: len(card.get("color_identity", [])) >= 4, "Does its color identity contain four or more colors?"),
        (lambda card: len(card_colors(card)) == 0, "Is the card itself colorless?"),
    ])

    # Use current "mana value" terminology and only meaningful thresholds.
    mana_values = sorted({card.get("cmc") for card in cards if card.get("cmc") is not None})
    for value in (value for value in mana_values if value <= 16):
        label = int(value) if float(value).is_integer() else value
        questions.append((lambda card, value=value: card.get("cmc") == value, f"Is its mana value exactly {label}?"))
    for threshold in range(1, 14):
        questions.append((lambda card, threshold=threshold: card.get("cmc", -1) < threshold, f"Is its mana value less than {threshold}?"))
        questions.append((lambda card, threshold=threshold: card.get("cmc", -1) >= threshold, f"Is its mana value {threshold} or greater?"))

    def has_cost_matching(card, pattern):
        return any(re.search(pattern, cost) for cost in mana_costs(card))

    questions.extend([
        (lambda card: has_cost_matching(card, r"\{X\}"), "Does its mana cost contain X?"),
        (lambda card: has_cost_matching(card, r"\{[^}]+/[^}]+\}"), "Does its mana cost contain a hybrid mana symbol?"),
        (lambda card: has_cost_matching(card, r"/P\}"), "Does its mana cost contain a Phyrexian mana symbol?"),
        (lambda card: has_cost_matching(card, r"\{\d+\}"), "Does its mana cost contain generic mana?"),
        (lambda card: sum(len(re.findall(r"\{[WUBRG](?:/[WUBRGP])?\}", cost)) for cost in mana_costs(card)) >= 2,
         "Does its mana cost contain at least two colored mana symbols?"),
    ])

    # These are structural rules-text questions, using explicit printed cues
    # rather than trying to infer a card's strategic meaning.
    triggered_ability_pattern = re.compile(
        r"(?:^|[\n.!?(\"“]\s*|[—;]\s*)(?:when|whenever|at)\b",
        re.IGNORECASE,
    )
    questions.extend([
        (lambda card: ":" in oracle_text(card),
         "Does its rules text contain an activated ability with a colon?"),
        (lambda card: bool(triggered_ability_pattern.search(oracle_text(card))),
         "Does it have a triggered ability beginning with “when,” “whenever,” or “at”?"),
        (lambda card: "•" in oracle_text(card),
         "Does its rules text include a bulleted choice?"),
    ])

    # Creature questions use the front creature face and support negative/decimal values.
    def creature_number(card, field):
        face = primary_creature_face(card)
        return numeric_value(face.get(field)) if face else None

    for field in ("power", "toughness"):
        for value in range(14):
            questions.append((lambda card, field=field, value=value: creature_number(card, field) == value,
                              f"Does its front creature face have {field} {value}?"))
        for threshold in (3, 4, 7):
            questions.append((lambda card, field=field, threshold=threshold: creature_number(card, field) is not None and creature_number(card, field) >= threshold,
                              f"Does its front creature face have {field} {threshold} or greater?"))

    def has_variable_pt(card):
        face = primary_creature_face(card)
        return bool(face) and any(face.get(field) is not None and numeric_value(face.get(field)) is None for field in ("power", "toughness"))

    def compare_pt(card, comparison):
        power, toughness = creature_number(card, "power"), creature_number(card, "toughness")
        return power is not None and toughness is not None and comparison(power, toughness)

    questions.extend([
        (has_variable_pt, "Does its front creature face have variable or nonnumeric power or toughness?"),
        (lambda card: compare_pt(card, lambda p, t: p == t), "Does its front creature face have equal power and toughness?"),
        (lambda card: compare_pt(card, lambda p, t: p > t), "Does its front creature face have greater power than toughness?"),
        (lambda card: compare_pt(card, lambda p, t: t > p), "Does its front creature face have greater toughness than power?"),
        (lambda card: compare_pt(card, lambda p, t: p + t >= 6), "Is the sum of its front creature face's power and toughness at least 6?"),
        (lambda card: compare_pt(card, lambda p, t: p + t >= 10), "Is the sum of its front creature face's power and toughness at least 10?"),
        (lambda card: creature_number(card, "power") is not None and creature_number(card, "power") > card.get("cmc", math.inf),
         "Is its front creature face's power greater than its mana value?"),
    ])

    # Explicit face/layout questions replace the old conflated double-face/split question.
    questions.append((lambda card: len(card.get("card_faces", [])) >= 2, "Does it have two or more faces or card halves?"))
    for layout, text in {
        "transform": "Is it a transforming double-faced card?",
        "modal_dfc": "Is it a modal double-faced card?",
        "adventure": "Is it an Adventure card?",
        "split": "Is it a split card?",
        "flip": "Is it a flip card?",
    }.items():
        questions.append((lambda card, layout=layout: card.get("layout") == layout, text))
    questions.extend([
        (lambda card: "Saga" in type_parts(card)[1], "Is it a Saga?"),
        (lambda card: "Class" in type_parts(card)[1], "Is it a Class?"),
    ])

    # Phrase supertypes, card types, and subtypes according to what they are.
    known_supertypes = {"basic", "legendary", "ongoing", "snow", "world"}
    left_variants, subtype_variants = {}, {}
    for card in cards:
        card_left, card_subtypes = type_parts(card)
        for word in card_left:
            left_variants.setdefault(word.lower(), set()).add(word)
        for word in card_subtypes:
            subtype_variants.setdefault(word.lower(), set()).add(word)
    for supertype in sorted(known_supertypes & left_variants.keys()):
        variants = left_variants[supertype]
        questions.append((lambda card, variants=variants: bool(type_parts(card)[0] & variants), f"Is it {supertype}?"))
    for card_type in sorted(left_variants.keys() - known_supertypes):
        variants = left_variants[card_type]
        questions.append((lambda card, variants=variants: bool(type_parts(card)[0] & variants), f"Is it {article(card_type)} {card_type}?"))
    for subtype in sorted(subtype_variants.keys() - {"saga", "class"}):
        variants = subtype_variants[subtype]
        label = min(variants, key=lambda value: (value.islower(), value))
        questions.append((lambda card, variants=variants: bool(type_parts(card)[1] & variants), f"Does it have the {label} subtype?"))

    for keyword in sorted({keyword for card in cards for keyword in card.get("keywords", [])}):
        questions.append((lambda card, keyword=keyword: keyword in card.get("keywords", []), f"Does it have the {keyword} keyword?"))

    questions.extend(semantic_questions)

    counts = Counter(text for _, text in questions)
    duplicates = sorted(text for text, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"Duplicate question text: {duplicates}")
    return questions


def load_semantic_questions(snapshot_path, config_path):
    with snapshot_path.open("r", encoding="utf-8") as file:
        snapshot = json.load(file)
    with config_path.open("r", encoding="utf-8") as file:
        config = json.load(file)

    tag_rows = snapshot.get("data")
    if not isinstance(tag_rows, list) or not tag_rows:
        raise ValueError(f"{snapshot_path} has no non-empty 'data' array")
    configured_questions = config.get("questions")
    if not isinstance(configured_questions, list) or not configured_questions:
        raise ValueError(f"{config_path} has no non-empty 'questions' array")

    oracle_ids_by_tag = {}
    for row in tag_rows:
        label, oracle_ids = row.get("label"), row.get("oracle_ids")
        if not isinstance(label, str) or not isinstance(oracle_ids, list):
            raise ValueError(f"Malformed Oracle tag row in {snapshot_path}")
        oracle_ids_by_tag.setdefault(label, set()).update(oracle_ids)

    seen_tags, seen_text = set(), set()
    questions = []
    for entry in configured_questions:
        tag, text = entry.get("tag"), entry.get("question")
        if not isinstance(tag, str) or not isinstance(text, str) or not text.strip():
            raise ValueError(f"Malformed semantic question in {config_path}: {entry}")
        if tag not in oracle_ids_by_tag:
            raise ValueError(f"Snapshot is missing configured tag: {tag}")
        if tag in seen_tags or text in seen_text:
            raise ValueError(f"Duplicate semantic tag or question in {config_path}: {entry}")
        seen_tags.add(tag)
        seen_text.add(text)
        tagged_ids = oracle_ids_by_tag[tag]
        questions.append((lambda card, tagged_ids=tagged_ids: card.get("oracle_id") in tagged_ids, text))

    provenance = {
        "snapshot": snapshot_path.name,
        "snapshot_metadata": snapshot.get("_snapshot_metadata", {}),
        "config": config_path.name,
        "config_version": config.get("version"),
        "semantic_question_count": len(questions),
    }
    return questions, provenance


def build_question_masks(cards, questions):
    masks = []
    for function, _ in questions:
        mask = 0
        for index, card in enumerate(cards):
            if function(card):
                mask |= 1 << index
        masks.append(mask)
    return masks


def build_tree(cards, questions):
    """Build using integer bitsets so full regeneration remains practical."""
    question_masks = build_question_masks(cards, questions)
    chosen_questions, leaf_counts = set(), Counter()

    def card_indices(mask):
        while mask:
            least_bit = mask & -mask
            yield least_bit.bit_length() - 1
            mask ^= least_bit

    def recurse(card_mask, available_ids, depth):
        card_count = card_mask.bit_count()
        if card_count == 1:
            leaf_counts[1] += 1
            return {"card_name": cards[next(card_indices(card_mask))]["name"], "depth": depth}

        best_id, best_balance = None, -1
        for question_id in available_ids:
            yes_count = (card_mask & question_masks[question_id]).bit_count()
            if yes_count in (0, card_count):
                continue
            # For unique records, maximum information gain is the closest split to half.
            balance = min(yes_count, card_count - yes_count)
            if balance > best_balance:
                best_id, best_balance = question_id, balance

        if best_id is None:
            leaf_counts[card_count] += 1
            return {"remainingPossibleCardNames": [cards[index]["name"] for index in card_indices(card_mask)], "depth": depth}

        chosen_questions.add(best_id)
        yes_mask = card_mask & question_masks[best_id]
        remaining_ids = tuple(qid for qid in available_ids if qid != best_id)
        return {"question": questions[best_id][1], "depth": depth,
                "yes": recurse(yes_mask, remaining_ids, depth + 1),
                "no": recurse(card_mask ^ yes_mask, remaining_ids, depth + 1)}

    tree = recurse((1 << len(cards)) - 1, tuple(range(len(questions))), 0)
    return tree, chosen_questions, leaf_counts


def name_word_count(name):
    """Count visible word-like units while keeping contractions together."""
    return len(re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?", name))


def evaluate_name_test(name, test):
    kind = test.get("kind")
    if kind == "word_count":
        return name_word_count(name) == test["value"]
    if kind == "apostrophe":
        return "'" in name or "’" in name
    if kind == "hyphen":
        return "-" in name
    if kind == "starts_with_vowel":
        return name[:1].casefold() in "aeiou"
    if kind == "alphabet_before":
        return name.casefold() < test["pivot"].casefold()
    raise ValueError(f"Unknown generated name test: {test}")


def expand_name_fallbacks(tree):
    """Replace semantic dead ends with deterministic printed-name questions."""
    statistics = Counter()

    def make_name_tree(raw_names, depth, group_size, used_tests=frozenset()):
        name_counts = Counter(raw_names)
        names = sorted(name_counts, key=str.casefold)
        if len(names) == 1:
            return {
                "card_name": names[0],
                "record_count": name_counts[names[0]],
                "depth": depth,
                "resolved_by": "name",
            }

        candidates = []
        word_counts = sorted({name_word_count(name) for name in names})
        for count in word_counts:
            key = ("word_count", count)
            if key in used_tests:
                continue
            test = {"kind": "word_count", "value": count}
            question = f"Does its name contain exactly {count} word{'s' if count != 1 else ''}?"
            candidates.append((key, test, question))
        for kind, question in (
            ("apostrophe", "Does its name contain an apostrophe?"),
            ("hyphen", "Does its name contain a hyphen?"),
            ("starts_with_vowel", "Does its name begin with a vowel?"),
        ):
            key = (kind, None)
            if key not in used_tests:
                candidates.append((key, {"kind": kind}, question))

        best = None
        for key, test, question in candidates:
            yes_names = [name for name in names if evaluate_name_test(name, test)]
            balance = min(len(yes_names), len(names) - len(yes_names))
            if balance and (best is None or balance > best[0]):
                best = (balance, key, test, question, yes_names)

        # Prefer a natural word/name-shape question when neither branch is tiny.
        if best is not None and best[0] >= max(1, math.ceil(len(names) * 0.3)):
            _, key, test, question, yes_names = best
            next_used = used_tests | {key}
        else:
            pivot = names[len(names) // 2]
            test = {"kind": "alphabet_before", "pivot": pivot}
            question = f"Ignoring capitalization, does its name come alphabetically before “{pivot}”?"
            yes_names = [name for name in names if evaluate_name_test(name, test)]
            next_used = used_tests

        yes_set = set(yes_names)
        yes_raw = [name for name in raw_names if name in yes_set]
        no_raw = [name for name in raw_names if name not in yes_set]
        if not yes_raw or not no_raw:
            raise ValueError(f"Generated name question did not split {names}: {question}")
        statistics["name_question_nodes"] += 1
        return {
            "question": question,
            "name_test": test,
            "phase": "name",
            "fallback_group_size": group_size,
            "depth": depth,
            "yes": make_name_tree(yes_raw, depth + 1, group_size, next_used),
            "no": make_name_tree(no_raw, depth + 1, group_size, next_used),
        }

    def walk(node):
        if "remainingPossibleCardNames" in node:
            names = node["remainingPossibleCardNames"]
            statistics["semantic_fallback_leaves"] += 1
            statistics["records_entering_name_phase"] += len(names)
            statistics["distinct_names_entering_name_phase"] += len(set(names))
            return make_name_tree(names, node["depth"], len(set(names)))
        if "question" in node:
            node["yes"] = walk(node["yes"])
            node["no"] = walk(node["no"])
        return node

    return walk(tree), dict(statistics)


def validate_tree(tree, expected_card_count):
    represented_count, internal_count = 0, 0

    def walk(node):
        nonlocal represented_count, internal_count
        if "card_name" in node:
            represented_count += node.get("record_count", 1)
        elif "remainingPossibleCardNames" in node:
            represented_count += len(node["remainingPossibleCardNames"])
        else:
            internal_count += 1
            if not node.get("question") or "yes" not in node or "no" not in node:
                raise ValueError(f"Malformed decision node: {node}")
            walk(node["yes"])
            walk(node["no"])

    walk(tree)
    if represented_count != expected_card_count:
        raise ValueError(f"Tree represents {represented_count} of {expected_card_count} records")
    return internal_count


def validate_card_routes(cards, tree, questions):
    question_map = {text: function for function, text in questions}
    failures = []
    for card in cards:
        node = tree
        while "question" in node:
            if node.get("phase") == "name":
                answer = evaluate_name_test(card["name"], node.get("name_test", {}))
            else:
                function = question_map.get(node["question"])
                if function is None:
                    failures.append(f"{card['name']}: missing predicate {node['question']!r}")
                    break
                answer = function(card)
            node = node["yes"] if answer else node["no"]
        else:
            names = [node["card_name"]] if "card_name" in node else node.get("remainingPossibleCardNames", [])
            if card["name"] not in names:
                failures.append(f"{card['name']}: reached wrong leaf")
    if failures:
        raise ValueError(f"Route validation failed ({len(failures)}): {'; '.join(failures[:5])}")


def calculate_tree_metrics(tree):
    depths = []
    leaf_sizes = Counter()
    internal_count = 0

    def walk(node):
        nonlocal internal_count
        if "card_name" in node:
            record_count = node.get("record_count", 1)
            size = 1
        elif "remainingPossibleCardNames" in node:
            size = len(node["remainingPossibleCardNames"])
        else:
            internal_count += 1
            walk(node["yes"])
            walk(node["no"])
            return
        leaf_sizes[size] += 1
        depths.extend([node["depth"]] * (record_count if "card_name" in node else size))

    walk(tree)
    ordered_depths = sorted(depths)
    return {
        "represented_records": len(depths),
        "decision_nodes": internal_count,
        "leaf_count": sum(leaf_sizes.values()),
        "single_card_leaves": leaf_sizes[1],
        "ambiguous_records": sum(size * count for size, count in leaf_sizes.items() if size > 1),
        "largest_leaf": max(leaf_sizes),
        "minimum_depth": min(depths),
        "maximum_depth": max(depths),
        "average_depth": round(statistics.mean(depths), 4),
        "median_depth": statistics.median(depths),
        "p90_depth": ordered_depths[int(0.9 * len(ordered_depths))],
        "leaf_sizes": {str(size): count for size, count in sorted(leaf_sizes.items())},
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Generate and validate the MTG guessing-game decision tree.")
    parser.add_argument("--card-data", type=Path, default=DEFAULT_CARD_DATA_FILE)
    parser.add_argument("--oracle-tags", type=Path, required=True, help="downloaded Oracle-tag snapshot")
    parser.add_argument("--semantic-config", type=Path, default=DEFAULT_SEMANTIC_CONFIG_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_TREE_FILE)
    parser.add_argument("--metrics-output", type=Path, default=DEFAULT_METRICS_FILE)
    return parser.parse_args()


def main():
    args = parse_args()
    with args.card_data.open("r", encoding="utf-8") as file:
        all_cards = json.load(file)
    exclusion_counts = Counter(
        reason for card in all_cards if (reason := normal_deck_card_exclusion(card))
    )
    cards = [card for card in all_cards if normal_deck_card_exclusion(card) is None]
    print(f"Included {len(cards):,} normal deck cards; excluded {len(all_cards) - len(cards):,} other game pieces.")
    for reason, count in exclusion_counts.most_common():
        print(f"  {reason}: {count:,}")
    semantic_questions, provenance = load_semantic_questions(args.oracle_tags, args.semantic_config)
    questions = make_questions(cards, semantic_questions)
    print(f"Building a tree for {len(cards):,} records with {len(questions):,} questions...")
    tree, chosen_ids, leaf_counts = build_tree(cards, questions)
    semantic_metrics = calculate_tree_metrics(tree)
    tree, name_fallback_statistics = expand_name_fallbacks(tree)
    internal_count = validate_tree(tree, len(cards))
    validate_card_routes(cards, tree, questions)
    metrics = calculate_tree_metrics(tree)
    chosen_question_text = {questions[index][1] for index in chosen_ids}
    semantic_statistics = []
    for function, text in semantic_questions:
        semantic_statistics.append({
            "question": text,
            "matching_included_records": sum(1 for card in cards if function(card)),
            "used_in_tree": text in chosen_question_text,
        })
    metrics.update({
        "card_data": args.card_data.name,
        "included_records": len(cards),
        "excluded_records": len(all_cards) - len(cards),
        "available_questions": len(questions),
        "questions_used": len(chosen_ids),
        "semantic_provenance": provenance,
        "semantic_questions": semantic_statistics,
        "semantic_phase": semantic_metrics,
        "name_fallback": name_fallback_statistics,
    })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as file:
        json.dump(tree, file, separators=(",", ":"))
    args.metrics_output.parent.mkdir(parents=True, exist_ok=True)
    with args.metrics_output.open("w", encoding="utf-8") as file:
        json.dump(metrics, file, indent=2)
        file.write("\n")
    print(f"Saved {args.output} with {internal_count:,} decision nodes.")
    print(f"Saved build metrics: {args.metrics_output}")
    print(f"Questions used somewhere in the tree: {len(chosen_ids):,}")
    print(
        "Depth: "
        f"average {metrics['average_depth']:.2f}, median {metrics['median_depth']}, "
        f"p90 {metrics['p90_depth']}, maximum {metrics['maximum_depth']}"
    )
    print(
        f"Semantic phase handed {semantic_metrics['ambiguous_records']:,} records "
        f"to name tiebreakers; largest group: {semantic_metrics['largest_leaf']}"
    )
    for size, count in sorted(leaf_counts.items()):
        print(f"{size} card{'s' if size != 1 else ''}: {count:,} leaves")


if __name__ == "__main__":
    main()
