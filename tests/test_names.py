"""Name correction after transcription (Jarvis #769): Parakeet takes no prompt,
so proper nouns are restored from the user's list after the fact."""
import pytest

from names import apply_replacements, correct_names, fix_joins, load_replacements

NAMES = ["HashDirectors", "SumSub", "FBAR", "Sølūnâ", "Dewald", "Telegram", "Matrix", "Whisper", "mlx"]


@pytest.mark.parametrize("heard, meant", [
    ("check the hash directors release", "check the HashDirectors release"),
    ("check the Hash Directors release", "check the HashDirectors release"),
    ("the hashdirectors board", "the HashDirectors board"),
    ("send the Sum Sub link", "send the SumSub link"),
    ("send the sum-sub link", "send the SumSub link"),
    ("file the fbar return", "file the FBAR return"),
    ("the solunâ plan", "the solunâ plan"),  # different letters: never guessed
])
def test_distinctive_names_are_rebuilt(heard, meant):
    assert fix_joins(heard, NAMES) == meant


@pytest.mark.parametrize("text", [
    "the matrix is clear",          # ordinary word that is also a name
    "whisper it to telegram later",  # plain capitalised names stay as heard
    "dewald said yes",
    "HashDirectors is right already",
])
def test_ordinary_words_and_plain_names_are_untouched(text):
    assert fix_joins(text, NAMES) == text


def test_never_joins_across_a_full_stop():
    assert fix_joins("we did the sum. Sub items next", NAMES) == "we did the sum. Sub items next"


def test_replacements_match_whole_words_ignoring_case():
    pairs = [("to Wald", "Dewald"), ("SomeSet", "SumSub")]
    assert apply_replacements("Also send to wald the SomeSet link", pairs) == "Also send Dewald the SumSub link"
    assert apply_replacements("go towald now", pairs) == "go towald now"


def test_replacement_text_is_literal(tmp_path):
    p = tmp_path / "r.txt"
    p.write_text("# comment\nbackslash => a\\\\b\n\nno arrow here\n")
    pairs = load_replacements(str(p))
    assert pairs == [("backslash", "a\\\\b")]
    assert apply_replacements("one backslash", pairs) == "one a\\\\b"


def test_missing_file_is_created_with_instructions(tmp_path):
    p = tmp_path / "sub" / "replacements.txt"
    assert load_replacements(str(p)) == []
    assert "heard => meant" in p.read_text()


def test_correct_names_runs_both_steps():
    assert correct_names("send to Wald the sum sub link", NAMES, [("to Wald", "Dewald")]) == "send Dewald the SumSub link"
