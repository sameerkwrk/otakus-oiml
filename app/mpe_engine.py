"""
Metrological calculation engine for OIML R-76 NAWI verification.

IMPORTANT (see project spec section 9.2):
The MPE zone boundaries below are the project's working table, not a
verbatim reproduction of a specific OIML R-76-1 edition. Before this
system is used for real legal-metrology decisions, an authorized
metrologist must confirm these values against the applicable official
edition of OIML R-76-1 for the jurisdiction in question. The table is
kept in one versioned place (this file) specifically so it can be
audited and swapped out without touching business logic elsewhere.
"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Optional, Tuple

# Bump this whenever MPE_ZONES or CLASS_N_RANGES changes. It is stamped
# onto every test report so old certificates remain traceable to the
# rule set that produced them, even if the table is edited later.
MPE_RULE_VERSION = "NAWI-Project-MPE-Table-v1 (approximate, pending official OIML R-76-1 sign-off)"

VALID_ACCURACY_CLASSES = ("I", "II", "III", "IIII")

# Zone boundaries expressed in units of e. Each tuple is
# (upper_bound_in_e_or_None_for_unbounded, mpe_in_e).
# Boundaries are inclusive of the upper bound (<=), matching the
# "|error| <= MPE" convention documented in the spec.
MPE_ZONES: Dict[str, List[Tuple[Optional[Decimal], Decimal]]] = {
    "I":    [(Decimal("50000"), Decimal("0.5")), (Decimal("200000"), Decimal("1.0")), (None, Decimal("1.5"))],
    "II":   [(Decimal("5000"),  Decimal("0.5")), (Decimal("20000"),  Decimal("1.0")), (None, Decimal("1.5"))],
    "III":  [(Decimal("500"),   Decimal("0.5")), (Decimal("2000"),   Decimal("1.0")), (None, Decimal("1.5"))],
    "IIII": [(Decimal("50"),    Decimal("0.5")), (Decimal("200"),    Decimal("1.0")), (None, Decimal("1.5"))],
}

# Legal number-of-verification-intervals (n = capacity / e) ranges per
# class. (min_n, max_n_or_None). Also an approximate working table —
# see module docstring.
CLASS_N_RANGES: Dict[str, Tuple[int, Optional[int]]] = {
    "I":    (50000, None),
    "II":   (100, 100000),
    "III":  (100, 10000),
    "IIII": (100, 1000),
}


class MetrologyValidationError(ValueError):
    """Raised for violations of metrological/legal constraints (as opposed
    to plain field-shape errors, which Pydantic already rejects)."""
    pass


class MetrologyEngine:

    # ---------------------------------------------------------------
    # Registration-time validation
    # ---------------------------------------------------------------

    @staticmethod
    def normalize_class(class_type: str) -> str:
        cls = class_type.upper().strip()
        if cls not in VALID_ACCURACY_CLASSES:
            raise MetrologyValidationError(
                f"'{class_type}' is not a valid accuracy class. Must be one of {VALID_ACCURACY_CLASSES}."
            )
        return cls

    @classmethod
    def calculate_n(cls, capacity: Decimal, e: Decimal) -> Decimal:
        if e <= 0:
            raise MetrologyValidationError("Verification scale interval 'e' must be greater than zero.")
        if capacity <= 0:
            raise MetrologyValidationError("Capacity (Max) must be greater than zero.")
        return capacity / e

    @classmethod
    def validate_instrument(cls, capacity: Decimal, d: Decimal, e: Decimal, accuracy_class: str) -> Decimal:
        """Runs full registration-time validation and returns n. Raises
        MetrologyValidationError on any violation."""
        acc_class = cls.normalize_class(accuracy_class)

        if d <= 0:
            raise MetrologyValidationError("Scale interval 'd' must be greater than zero.")
        if e <= 0:
            raise MetrologyValidationError("Verification scale interval 'e' must be greater than zero.")
        if e < d:
            raise MetrologyValidationError(
                "Verification scale interval 'e' cannot be smaller than the scale interval 'd' (e >= d)."
            )
        if e % d != 0:
            raise MetrologyValidationError(
                f"'e' ({e}) must be a whole-number multiple of 'd' ({d})."
            )

        n = cls.calculate_n(capacity, e)

        if capacity % e != 0:
            raise MetrologyValidationError(
                f"Capacity ({capacity}) must be an exact multiple of 'e' ({e}) so that n = capacity/e is a whole number."
            )

        min_n, max_n = CLASS_N_RANGES[acc_class]
        n_int = int(n)
        if n_int < min_n or (max_n is not None and n_int > max_n):
            bound = f"{min_n}\u2013{max_n}" if max_n is not None else f">= {min_n}"
            raise MetrologyValidationError(
                f"n = {n_int} is outside the permitted range for Class {acc_class} (expected {bound}). "
                f"Adjust capacity, e, or accuracy class."
            )

        return n

    # ---------------------------------------------------------------
    # MPE lookup
    # ---------------------------------------------------------------

    @classmethod
    def get_mpe_in_e(cls, class_type: str, load_in_e: Decimal) -> Decimal:
        acc_class = cls.normalize_class(class_type)
        abs_load = abs(load_in_e)
        zones = MPE_ZONES[acc_class]
        for upper_bound, mpe_in_e in zones:
            if upper_bound is None or abs_load <= upper_bound:
                return mpe_in_e
        # Unreachable: last zone always has upper_bound=None
        return zones[-1][1]

    @classmethod
    def get_mpe_value(cls, class_type: str, reference_load: Decimal, e: Decimal) -> Decimal:
        reference_in_e = reference_load / e
        mpe_in_e = cls.get_mpe_in_e(class_type, reference_in_e)
        return mpe_in_e * e

    # ---------------------------------------------------------------
    # Observation evaluation
    # ---------------------------------------------------------------

    @classmethod
    def evaluate_observation(
        cls,
        reference_load: Decimal,
        observed_value: Decimal,
        e: Decimal,
        accuracy_class: str,
    ) -> Dict[str, Any]:
        """Evaluate a single accuracy/eccentricity/environmental observation.
        Comparison is |error| <= MPE (inclusive), evaluated in exact Decimal
        arithmetic with no intermediate rounding."""
        error = observed_value - reference_load
        error_in_e = error / e if e != 0 else Decimal("0")

        mpe_value = cls.get_mpe_value(accuracy_class, reference_load, e)

        passed = abs(error) <= mpe_value

        return {
            "reference_load": reference_load,
            "observed_value": observed_value,
            "calculated_error": error,
            "error_in_e": error_in_e,
            "mpe": mpe_value,
            "status": "PASS" if passed else "FAIL",
        }

    @classmethod
    def evaluate_repeatability(
        cls,
        observations: List[Decimal],
        repeatability_load: Decimal,
        e: Decimal,
        accuracy_class: str,
    ) -> Dict[str, Any]:
        """Repeatability concerns variation *among* repeated readings, not
        closeness of any single reading to the reference (spec 5.2).
        The tolerance for max-min spread is the MPE applicable at the
        tested load -- NOT a flat 1e.
        """
        if not observations or len(observations) < 2:
            raise MetrologyValidationError("At least 2 observations required for repeatability calculation.")

        min_val = min(observations)
        max_val = max(observations)
        diff = max_val - min_val

        allowed_mpe = cls.get_mpe_value(accuracy_class, repeatability_load, e)
        passed = diff <= allowed_mpe

        return {
            "min": min_val,
            "max": max_val,
            "difference": diff,
            "allowed_mpe": allowed_mpe,
            "status": "PASS" if passed else "FAIL",
        }
        
    @classmethod
    def evaluate_discrimination(
        cls,
        reference_load: Decimal,
        d: Decimal,
        initial_indication: Decimal,
        final_indication: Decimal,
    ) -> Dict[str, Any]:
        """OIML R-76 3.8.2.2: An additional load of 1.4d placed gently on the 
        instrument at equilibrium shall change the indication by >= 1d."""
        change = final_indication - initial_indication
        passed = change >= d
        
        return {
            "calculated_change": change,
            "status": "PASS" if passed else "FAIL",
        }