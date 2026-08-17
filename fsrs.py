"""FSRS-4.5 — an optional alternative scheduler to the built-in SM-2.

Pure functions over (stability, difficulty, elapsed_days, grade). No database, no
dependencies, no clock: everything here is arithmetic, which is what makes it
testable against the published algorithm rather than against our own behaviour.

Where it differs from SM-2, and why it is offered:
  SM-2 multiplies the interval by an ease factor that only ever ratchets on the
  card's grade history. FSRS models two separate quantities — how long a memory
  currently lasts (stability) and how hard this particular card is (difficulty) —
  and schedules for an explicit target retention. The practical effect is that a
  card you keep getting right leaves faster, and a card you keep lapsing on stops
  being flung to the far future.

SM-2 remains the default. This is opt-in per D-x: an existing deck must not have
its scheduling silently changed underneath it.

Reference: FSRS-4.5 (open-spaced-repetition). Grades are the app's four buttons
mapped to 1..4 = again/hard/good/easy — note this is NOT srs.QUALITY (1/3/4/5),
which is SM-2's 0-5 scale; the mapping lives in srs.py and is asserted by a test.
"""

import math

# FSRS-4.5 default weights, in the published order.
DEFAULT_W = (
    0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.0310,
    1.6474, 0.1367, 1.0461, 2.1072, 0.0793, 0.3246, 1.5870, 0.2272, 2.8755,
)

DECAY = -0.5
FACTOR = 19.0 / 81.0          # so that R(t=S) == 0.9

MIN_DIFFICULTY, MAX_DIFFICULTY = 1.0, 10.0
MIN_STABILITY = 0.1
MAX_INTERVAL = 36500          # 100 years: a cap, not a schedule

AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def retrievability(elapsed_days, stability):
    """Probability of recall now, given days since the last review.

    A power curve, not an exponential: FSRS-4.5 switched to it because real
    forgetting has a fatter tail than e^-t/S predicts.
    """
    if stability <= 0:
        return 0.0
    return (1.0 + FACTOR * max(0.0, elapsed_days) / stability) ** DECAY


def initial_stability(grade, w=DEFAULT_W):
    """First-ever review: stability comes straight from the weights."""
    return _clamp(w[_grade_index(grade)], MIN_STABILITY, float(MAX_INTERVAL))


def initial_difficulty(grade, w=DEFAULT_W):
    """FSRS-4.5's linear form: D_0(G) = w4 - (G-3)*w5, so a first 'good' lands on
    w4 itself (~5.2 of 10) and the other buttons step either side of it.

    NOT the exponential form from FSRS-5 — that one is paired with FSRS-5's own
    weights, and feeding it these gives D_0(good) = -5.5, which clamps to 1.0 and
    silently marks every new card as the easiest possible.
    """
    return _clamp(w[4] - (grade - 3.0) * w[5],
                  MIN_DIFFICULTY, MAX_DIFFICULTY)


def _grade_index(grade):
    if grade not in (AGAIN, HARD, GOOD, EASY):
        raise ValueError("grade must be 1..4, got %r" % (grade,))
    return grade - 1


def next_difficulty(difficulty, grade, w=DEFAULT_W):
    """Difficulty drifts on each answer and is pulled back toward the 'easy'
    baseline, so one bad day cannot permanently brand a card as hard."""
    delta = difficulty - w[6] * (grade - 3.0)
    target = initial_difficulty(EASY, w)
    return _clamp(w[7] * target + (1.0 - w[7]) * delta,
                  MIN_DIFFICULTY, MAX_DIFFICULTY)


def _stability_after_recall(stability, difficulty, r, grade, w=DEFAULT_W):
    hard_penalty = w[15] if grade == HARD else 1.0
    easy_bonus = w[16] if grade == EASY else 1.0
    growth = (math.exp(w[8])
              * (11.0 - difficulty)
              * (stability ** -w[9])
              * (math.expm1(w[10] * (1.0 - r)))
              * hard_penalty
              * easy_bonus)
    return stability * (1.0 + growth)


def _stability_after_lapse(stability, difficulty, r, w=DEFAULT_W):
    return (w[11]
            * (difficulty ** -w[12])
            * ((stability + 1.0) ** w[13] - 1.0)
            * math.exp(w[14] * (1.0 - r)))


def next_state(stability, difficulty, elapsed_days, grade, w=DEFAULT_W):
    """The core step. stability/difficulty None = this card's first review.

    Returns (stability, difficulty) for after this answer.
    """
    _grade_index(grade)
    if stability is None or difficulty is None:
        return initial_stability(grade, w), initial_difficulty(grade, w)

    r = retrievability(elapsed_days, stability)
    d = next_difficulty(difficulty, grade, w)
    if grade == AGAIN:
        s = _stability_after_lapse(stability, difficulty, r, w)
        # A lapse must not make a card MORE stable than it already was.
        s = min(s, stability)
    else:
        s = _stability_after_recall(stability, difficulty, r, grade, w)
    return _clamp(s, MIN_STABILITY, float(MAX_INTERVAL)), d


def interval_for(stability, target_retention=0.9):
    """Days until retrievability decays to the target. Always >= 1: this app has
    no intra-day learning steps, so the soonest anything returns is tomorrow."""
    if not 0.0 < target_retention < 1.0:
        raise ValueError("target_retention must be between 0 and 1")
    days = (stability / FACTOR) * (target_retention ** (1.0 / DECAY) - 1.0)
    return int(_clamp(round(days), 1, MAX_INTERVAL))


def review(stability, difficulty, elapsed_days, grade, target_retention=0.9,
           w=DEFAULT_W):
    """One answer -> (stability, difficulty, interval_days)."""
    s, d = next_state(stability, difficulty, elapsed_days, grade, w)
    return s, d, interval_for(s, target_retention)
