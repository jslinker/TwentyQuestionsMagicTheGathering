import math
import json

# --- Global set to track questions chosen as optimal splits ---
# This set will store the (function, string) tuple of each question
# that is selected as the best split at any node in the tree.
all_chosen_questions_set = set()

# --- Global counters for leaf node types ---
# These will track how many leaf nodes end up with a specific number of cards.
leaf_node_counts = {
    "1_card": 0,
    "2_cards": 0,
    "3_cards": 0,
    "more_than_3_cards": 0
}

# --- 1. SAMPLE DATASET AND QUESTIONS ---
# A list of functions that represent our yes/no questions.
# Each function takes a card object and returns True or False.
# This list is what our algorithm will iterate through to find the best question.

# --- Original (non-type, non-keyword) questions ---
def has_red_color(card):
    try:
        return "R" in card.get("color_identity", [])
    except KeyError:
        return False

def has_blue_color(card):
    try:
        return "U" in card.get("color_identity", [])
    except KeyError:
        return False

def has_green_color(card):
    try:
        return "G" in card.get("color_identity", [])
    except KeyError:
        return False

def has_black_color(card):
    try:
        return "B" in card.get("color_identity", [])
    except KeyError:
        return False

def has_white_color(card):
    try:
        return "W" in card.get("color_identity", [])
    except KeyError:
        return False

def has_colorless_color(card):
    # A card is colorless if it has no colors and is not a land (lands often have no colors but aren't "colorless" in the same sense as artifacts)
    try:
        return not card.get("colors", []) and "Land" not in card.get("type_line", "")
    except KeyError:
        return False
        
def is_legendary(card):
    # Checks if "Legendary" is in the type_line (e.g., "Legendary Creature")
    try:
        return "Legendary" in card.get("type_line", "")
    except KeyError:
        return False

# --- Dynamically generated question functions for CMC, Power, and Toughness ---

# CMC questions (0-13 exact values, and various ranges)
_generated_cmc_questions = []
for i in range(14): # CMC from 0 to 13
    def make_cmc_exact_question(cmc_val):
        def check_cmc(card):
            try:
                return card.get("cmc") == cmc_val
            except KeyError:
                return False
        return check_cmc
    _generated_cmc_questions.append((make_cmc_exact_question(i), f"Does the card have a converted mana value (CMC) of {i}?"))

_generated_cmc_questions.extend([
    (lambda card: card.get("cmc", -1) < 3, "Does the card have a converted mana value (CMC) less than 3?"),
    (lambda card: card.get("cmc", -1) < 5, "Does the card have a converted mana value (CMC) less than 5?"),
    (lambda card: card.get("cmc", -1) < 7, "Does the card have a converted mana value (CMC) less than 7?"),
    (lambda card: card.get("cmc", -1) >= 3, "Does the card have a converted mana value (CMC) of 3 or more?"),
    (lambda card: card.get("cmc", -1) >= 5, "Does the card have a converted mana value (CMC) of 5 or more?"),
    (lambda card: card.get("cmc", -1) >= 7, "Does the card have a converted mana value (CMC) of 7 or more?"),
    (lambda card: card.get("cmc", -1) >= 10, "Does the card have a converted mana value (CMC) of 10 or more?"),
])

# Helper for power/toughness checks to avoid repetition
def _is_creature_with_numeric_pt(card, pt_field):
    # This helper checks if the card is a creature and has a numeric power/toughness
    try:
        # Re-using the dynamic type check for 'Creature'
        # Note: 'Creature' is now dynamically generated, so we check for its presence in type_line.
        return "Creature" in card.get("type_line", "") and \
               card.get(pt_field) is not None and \
               str(card[pt_field]).isdigit()
    except KeyError:
        return False

# Power questions (0-13 exact values, and various ranges)
_generated_power_questions = []
for i in range(14): # Power from 0 to 13
    def make_power_exact_question(power_val):
        def check_power(card):
            if _is_creature_with_numeric_pt(card, "power"):
                return int(card["power"]) == power_val
            return False
        return check_power
    _generated_power_questions.append((make_power_exact_question(i), f"Is it a creature with power {i}?"))

_generated_power_questions.extend([
    (lambda card: _is_creature_with_numeric_pt(card, "power") and int(card["power"]) < 3, "Is it a creature with power less than 3?"),
    (lambda card: _is_creature_with_numeric_pt(card, "power") and int(card["power"]) >= 4, "Is it a creature with power 4 or greater?"),
    (lambda card: _is_creature_with_numeric_pt(card, "power") and int(card["power"]) >= 7, "Is it a creature with power 7 or greater?"),
])

# Toughness questions (0-13 exact values, and various ranges)
_generated_toughness_questions = []
for i in range(14): # Toughness from 0 to 13
    def make_toughness_exact_question(toughness_val):
        def check_toughness(card):
            if _is_creature_with_numeric_pt(card, "toughness"):
                return int(card["toughness"]) == toughness_val
            return False
        return check_toughness
    _generated_toughness_questions.append((make_toughness_exact_question(i), f"Is it a creature with toughness {i}?"))

_generated_toughness_questions.extend([
    (lambda card: _is_creature_with_numeric_pt(card, "toughness") and int(card["toughness"]) < 3, "Is it a creature with toughness less than 3?"),
    (lambda card: _is_creature_with_numeric_pt(card, "toughness") and int(card["toughness"]) >= 4, "Is it a creature with toughness 4 or greater?"),
    (lambda card: _is_creature_with_numeric_pt(card, "toughness") and int(card["toughness"]) >= 7, "Is it a creature with toughness 7 or greater?"),
])

# --- Dynamically generated questions for card types ---
# This will be populated after loading card_data to find all unique types.
_generated_type_questions = []

# --- Dynamically generated questions for keywords ---
# This will be populated after loading card_data to find all unique keywords.
_generated_keyword_questions = []


# A list of tuples, where each tuple contains a function and a human-readable question string.
# This makes it easy for the tree to store and display the questions.
# This list will be fully assembled after data loading.
available_questions = []


# --- 2. CORE ALGORITHMS ---

def calculate_entropy(cards):
    """
    Calculates the entropy of a list of cards.
    
    OPTIMIZED VERSION: Assumes all card names in the input 'cards' list are unique.
    If this guarantee holds, entropy is simply log2(N) where N is the number of cards.
    """
    total_cards = len(cards)
    if total_cards == 0:
        return 0.0
    
    # If all card names are guaranteed unique, entropy is simply log2(N)
    return math.log2(total_cards)

def calculate_information_gain(cards, question_function):
    """
    Calculates the information gain for a given question.
    
    Information Gain measures the reduction in entropy after a dataset is split
    by a question. The higher the information gain, the better the question
    is at creating purer subsets.
    
    Formula:
    $IG(S, A) = E(S) - \sum_{v \in V(A)} \frac{|S_v|}{|S|} E(S_v)$
    where E(S) is the entropy of the current set, |Sv|/|S| is the weighting
    of the new subset, and E(Sv) is the entropy of that new subset.
    
    Args:
        cards (list): The list of card objects to split.
        question_function (function): A function that takes a card and returns True/False.
    
    Returns:
        float: The information gain value.
    """
    original_entropy = calculate_entropy(cards)
    
    # Split the cards into 'yes' and 'no' subsets
    yes_cards = [card for card in cards if question_function(card)]
    no_cards = [card for card in cards if not question_function(card)]

    # If a split results in an empty set for either yes or no, it1 means this question
    # doesn't partition the data effectively, so its gain is 0.
    if not yes_cards or not no_cards:
        return 0.0

    weighted_entropy = (
        (len(yes_cards) / len(cards)) * calculate_entropy(yes_cards) +
        (len(no_cards) / len(cards)) * calculate_entropy(no_cards)
    )

    information_gain = original_entropy - weighted_entropy
    return information_gain

def find_optimal_question(cards, questions):
    """
    Finds the question with the highest information gain.
    
    Args:
        cards (list): The current set of cards.
        questions (list): The list of available (function, string) question tuples.
    
    Returns:
        tuple: The optimal question (function, string) and its information gain.
    """
    best_question = None
    max_gain = -1.0
    
    for question_func, question_str in questions:
        # Avoid questions that don't split the data at all.
        yes_cards = [card for card in cards if question_func(card)]
        no_cards = [card for card in cards if not question_func(card)]
        
        # A question must partition the data into non-empty sets to be considered.
        if not yes_cards or not no_cards:
            continue
            
        gain = calculate_information_gain(cards, question_func)
        
        if gain > max_gain:
            max_gain = gain
            best_question = (question_func, question_str)
            
    return best_question, max_gain

# --- 3. RECURSIVE TREE BUILDING FUNCTION ---

def build_tree(cards, available_questions, depth=0):
    """
    Recursively builds a decision tree using the Information Gain algorithm.
    
    Args:
        cards (list): The list of cards for the current node.
        available_questions (list): The list of questions not yet used in this branch.
        depth (int): The current depth of the tree (for debugging).
    
    Returns:
        dict: A dictionary representing a node in the decision tree.
    """
    global leaf_node_counts # Declare use of global variable
    
    # Base Case 1: Only one card remains in the list.
    # We've reached a leaf node, so we return the final card.
    if len(cards) == 1:
        leaf_node_counts["1_card"] += 1
        return {
            "card_name": cards[0]["name"],
            "depth": depth
        }

    # Base Case 2: No more questions to ask, or no question can provide further gain.
    # This means the algorithm couldn't find a question to distinguish remaining cards.
    # We return a leaf node with the remaining possibilities.
    optimal_q_result = find_optimal_question(cards, available_questions)
    if not available_questions or optimal_q_result[0] is None or optimal_q_result[1] <= 0:
        num_remaining_cards = len(cards)
        if num_remaining_cards == 2:
            leaf_node_counts["2_cards"] += 1
        elif num_remaining_cards == 3:
            leaf_node_counts["3_cards"] += 1
        else: # num_remaining_cards > 3
            leaf_node_counts["more_than_3_cards"] += 1

        return {
            "remainingPossibleCardNames": [card["name"] for card in cards],
            "depth": depth
        }

    # Recursive Step: Find the optimal question and split the data.
    optimal_question_func, optimal_question_str = optimal_q_result[0] # Get function and string

    # Add the chosen question to the global set
    global all_chosen_questions_set
    all_chosen_questions_set.add(optimal_q_result[0]) # Add the (func, str) tuple

    # Filter the questions for the next recursive call to avoid asking
    # the same question twice on the same branch.
    remaining_questions = [q for q in available_questions if q != optimal_q_result[0]]

    # Split the cards for the next branches
    yes_cards = [card for card in cards if optimal_question_func(card)]
    no_cards = [card for card in cards if not optimal_question_func(card)]

    # Build the 'yes' and 'no' subtrees recursively
    yes_branch = build_tree(yes_cards, remaining_questions, depth + 1)
    no_branch = build_tree(no_cards, remaining_questions, depth + 1)

    # Return the current decision node (internal node, no possibleCardNames needed here)
    return {
        "question": optimal_question_str,
        "depth": depth,
        "yes": yes_branch,
        "no": no_branch
    }

# --- 4. MAIN EXECUTION ---
if __name__ == "__main__":
    # Load card data from an external JSON file.
    try:
        with open('card-data.json', 'r') as f:
            card_data = json.load(f)
    except FileNotFoundError:
        print("Error: 'card-data.json' not found. Please create the file.")
        exit(1)
    except json.JSONDecodeError:
        print("Error: 'card-data.json' contains invalid JSON.")
        exit(1)
    
    print("Skipping oracle_id uniqueness validation as requested. Proceeding with tree building. ✅")

    # --- Dynamically generate type questions based on loaded data ---
    all_unique_types = set()
    for card in card_data:
        type_line = card.get("type_line", "")
        # Split by ' — ' (em dash) to treat subtypes/supertypes uniformly
        # and then split by spaces to get all individual type components
        for type_word in type_line.replace(' — ', ' ').split():
            if type_word: # Ensure it's not an empty string
                all_unique_types.add(type_word)
    
    _generated_type_questions = []
    for card_type in sorted(list(all_unique_types)):
        def make_type_question(c_type):
            def check_type(card):
                try:
                    return c_type in card.get("type_line", "")
                except KeyError:
                    return False
            return check_type
        _generated_type_questions.append((make_type_question(card_type), f"Is the card a {card_type}?"))

    # --- Dynamically generate keyword questions based on loaded data ---
    all_unique_keywords = set()
    for card in card_data:
        keywords = card.get("keywords", [])
        for keyword in keywords:
            if keyword: # Ensure keyword is not empty string
                all_unique_keywords.add(keyword)
    
    _generated_keyword_questions = []
    for keyword in sorted(list(all_unique_keywords)):
        def make_keyword_question(kw):
            def check_keyword(card):
                try:
                    return kw in card.get("keywords", [])
                except KeyError:
                    return False
            return check_keyword
        _generated_keyword_questions.append((make_keyword_question(keyword), f"Does the card have the {keyword} keyword?"))


    # Assemble the final available_questions list
    available_questions.extend([
        (has_red_color, "Does the card have Red in its color identity?"),
        (has_blue_color, "Does the card have Blue in its color identity?"),
        (has_green_color, "Does the card have Green in its color identity?"),
        (has_black_color, "Does the card have Black in its color identity?"),
        (has_white_color, "Does the card have White in its color identity?"),
        (has_colorless_color, "Is the card Colorless?"),
        (is_legendary, "Is the card Legendary?"),
    ])
    available_questions.extend(_generated_cmc_questions)
    available_questions.extend(_generated_power_questions)
    available_questions.extend(_generated_toughness_questions)
    available_questions.extend(_generated_type_questions) # Add dynamic type questions
    available_questions.extend(_generated_keyword_questions) # Add dynamic keyword questions

    # Build the tree from the full (or filtered, if uncommented) data
    decision_tree = build_tree(card_data, available_questions)
    
    # Save the resulting tree to a JSON file instead of printing to the console
    try:
        with open('decision-tree.json', 'w') as f:
            json.dump(decision_tree, f, indent=4)
        print("\nSuccessfully built decision tree and saved it to 'decision-tree.json'")
    except IOError:
        print("Error: Could not write to 'decision-tree.json'.")
        exit(1)

    # --- Log questions never chosen as optimal splits ---
    # Extract just the string part of all available questions for comparison
    all_available_question_strings = {q_str for q_func, q_str in available_questions}
    
    # Extract just the string part of all chosen questions
    all_chosen_question_strings = {q_str for q_func, q_str in all_chosen_questions_set}

    unused_question_strings = sorted(list(all_available_question_strings - all_chosen_question_strings))
    
    if unused_question_strings:
        print("\nQuestions never chosen as optimal splits:")
        for q_str in unused_question_strings:
            print(f"- {q_str}")
    else:
        print("\nAll available questions were chosen as optimal splits at some point in the tree! 🎉")

    # --- Log leaf node counts ---
    print("\n--- Leaf Node Summary ---")
    print(f"Leaf nodes with 1 card: {leaf_node_counts['1_card']}")
    print(f"Leaf nodes with 2 cards: {leaf_node_counts['2_cards']}")
    print(f"Leaf nodes with 3 cards: {leaf_node_counts['3_cards']}")
    print(f"Leaf nodes with more than 3 cards: {leaf_node_counts['more_than_3_cards']}")
    print("-------------------------")
