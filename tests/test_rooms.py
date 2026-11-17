"""The room primitives — the security boundary of this service.

Creation, lookup and membership need a database and are exercised end to end by
tests/smoke.mjs.
What is here is everything that can be wrong without a database being involved, which
is most of what matters: a guessable code, a leaky comparison, a code that will not
match what a student typed.
"""

import secrets
import string

from classroom_sync.rooms import (
    ROOM_CODE_ALPHABET,
    ROOM_CODE_LENGTH,
    new_room_code,
    normalise_code,
    password_matches,
)


def test_the_alphabet_avoids_characters_that_misread_on_a_whiteboard():
    """Excludes I, L, O, 0 and 1 — the pairs that misread from the back of a room.

    S and 5 are **both present**, deliberately recorded here because the original
    contract claimed otherwise and this test is what caught it. Changing the alphabet
    now would be a design change in the middle of a port; if S/5 proves confusing in a
    real class, change it then, knowingly.
    """
    for confusable in "ILO01":
        assert confusable not in ROOM_CODE_ALPHABET
    assert "S" in ROOM_CODE_ALPHABET and "5" in ROOM_CODE_ALPHABET


def test_codes_are_the_right_shape():
    for _ in range(50):
        code = new_room_code()
        assert len(code) == ROOM_CODE_LENGTH
        assert set(code) <= set(ROOM_CODE_ALPHABET)


def test_codes_do_not_repeat_in_a_small_sample():
    """Not a randomness proof — a canary for someone swapping in a seeded generator."""
    codes = {new_room_code() for _ in range(500)}
    assert len(codes) > 490


def test_the_code_generator_uses_secrets_not_random():
    """`random` is seedable and predictable; a guessable code is a stranger in class.

    Asserted by behaviour: seeding `random` must not make codes reproducible.
    """
    import random

    random.seed(1234)
    first = [new_room_code() for _ in range(5)]
    random.seed(1234)
    assert first != [new_room_code() for _ in range(5)]


def test_password_comparison_handles_the_empty_case():
    assert password_matches("hunter2", "hunter2")
    assert not password_matches("", "hunter2")
    assert not password_matches("hunter2", "")


def test_a_student_typing_lowercase_finds_the_room():
    assert normalise_code(" abc123 ") == "ABC123"
    assert normalise_code("AbC123") == "ABC123"


def test_comparisons_are_constant_time():
    """`==` on a secret leaks its prefix through timing. Asserted structurally: the
    comparison must route through hmac.compare_digest."""
    import inspect

    from classroom_sync import rooms

    assert "compare_digest" in inspect.getsource(rooms.password_matches)
