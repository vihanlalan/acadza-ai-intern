"""Safety-gate tests. Run: python -m unittest engine.test_guards -v

These pin down the promises the console shows to faculty: scope is enforced,
`find` must match exactly once, key flips need consensus, an adversarial
comment can never trigger a write, and auto:false repairs are never written.
"""
import json
import unittest

from .data import load_all, view
from .guards import gate
from .scanners import comment_signals, parse_options, question_signals

QS, REPORTS, TAX = load_all()
V = {k: view(q) for k, q in QS.items()}
LCR = "60f744b3f1b9bc7cc7b4a949"     # key C, solution and physics say A (50 V)
SOLVE_A = {"final_answer": "A"}


def prop(**kw):
    base = {"verdict": "defect", "repair": "answer_key", "edit": {"field": "answer", "find": "C", "replace": "A"},
            "route_request": "auto_apply", "confidence": 0.95}
    base.update(kw)
    return base


class Gate(unittest.TestCase):
    def test_key_flip_with_consensus_auto_applies(self):
        g = gate(prop(), V[LCR], TAX, SOLVE_A, "A", [])
        self.assertEqual(g["route"], "auto_apply")
        self.assertEqual(g["after"]["answer"], "A")

    def test_solution_conclusion_given_as_value_maps_to_option(self):
        # regression: Groq wrote solution_concludes="50 V" instead of "A"
        self.assertEqual(gate(prop(), V[LCR], TAX, {"final_answer": "50 V"}, "50 V", [])["route"], "auto_apply")
        # integer question: "q/2e0 -> n = 1" style values compare as numbers
        g = gate(prop(edit={"field": "answer", "find": "2", "replace": "1"}), V["68dea616d0cda73d07e93ae6"], TAX,
                 {"final_answer": "1"}, "n = 1", [])
        self.assertEqual(g["route"], "auto_apply")
        # a value that matches no option is NOT treated as agreement
        self.assertEqual(gate(prop(), V[LCR], TAX, SOLVE_A, "55 V", [])["route"], "faculty_review")

    def test_key_flip_without_solution_agreement_goes_to_human(self):
        g = gate(prop(), V[LCR], TAX, SOLVE_A, "C", [])
        self.assertEqual(g["route"], "faculty_review")

    def test_key_flip_without_solver_agreement_goes_to_human(self):
        g = gate(prop(), V[LCR], TAX, {"final_answer": "B"}, "A", [])
        self.assertEqual(g["route"], "faculty_review")

    def test_adversarial_comment_never_writes(self):
        g = gate(prop(), V[LCR], TAX, SOLVE_A, "A", ["prompt_injection"])
        self.assertEqual(g["route"], "faculty_review")

    def test_field_outside_repair_scope_is_blocked(self):
        e = {"field": "answer", "find": "C", "replace": "A"}
        g = gate(prop(repair="spelling", edit=e), V[LCR], TAX, SOLVE_A, "A", [])
        self.assertEqual(g["route"], "faculty_review")
        self.assertTrue(any(not c["ok"] and "allowed" in c["name"] for c in g["checks"]))

    def test_find_must_match_exactly_once(self):
        e = {"field": "question", "find": "V", "replace": "W"}  # 'V' occurs many times
        g = gate(prop(repair="spelling", edit=e), V[LCR], TAX, SOLVE_A, "A", [])
        self.assertEqual(g["route"], "faculty_review")
        e = {"field": "question", "find": "not in the record", "replace": "x"}
        g = gate(prop(repair="spelling", edit=e), V[LCR], TAX, SOLVE_A, "A", [])
        self.assertEqual(g["route"], "faculty_review")

    def test_auto_false_repair_is_never_written(self):
        e = {"field": "question", "find": "(C) 130 V", "replace": "(C) 90 V"}
        g = gate(prop(repair="identical_options", edit=e), V[LCR], TAX, SOLVE_A, "A", [])
        self.assertEqual(g["route"], "faculty_review")
        self.assertIsNone(g["edit"])
        self.assertEqual(g["suggestion"], e)

    def test_non_defect_carries_no_edit(self):
        g = gate(prop(verdict="no_error"), V[LCR], TAX, SOLVE_A, "A", [])
        self.assertEqual((g["route"], g["repair"], g["edit"]), ("no_change", None, None))

    def test_unknown_verdict_or_repair_rejected(self):
        self.assertEqual(gate(prop(verdict="corrected"), V[LCR], TAX, SOLVE_A, "A", [])["verdict"], "not_actionable")
        self.assertEqual(gate(prop(repair="rewrite"), V[LCR], TAX, SOLVE_A, "A", [])["route"], "faculty_review")

    def test_low_confidence_goes_to_human(self):
        self.assertEqual(gate(prop(confidence=0.6), V[LCR], TAX, SOLVE_A, "A", [])["route"], "faculty_review")

    def test_edit_that_breaks_markup_is_blocked(self):
        q = "63765a28f1d8416ae0408d89"
        e = {"field": "question", "find": "60 days", "replace": "60 \\frac{d}{a} days"}
        g = gate(prop(repair="spelling", edit=e), V[q], TAX, {"final_answer": "C"}, "C", [])
        self.assertEqual(g["route"], "faculty_review")


class Scanners(unittest.TestCase):
    def test_planted_structure_defects(self):
        codes = lambda qid: {s["code"] for s in question_signals(V[qid])}
        self.assertIn("option_blank", codes("68e872546e45ba37dcc16bc0"))
        self.assertIn("option_missing", codes("622f08d7b70e4b50e4500a1b"))
        self.assertIn("option_duplicate", codes("6956d79788d7701e5617bc29"))
        self.assertIn("answer_malformed", codes("68c8153cfb6b2f16e2d3ceaf"))
        self.assertIn("answer_malformed", codes("68f21caf941cda588bc04365"))
        self.assertIn("latex_leak", codes("63765a28f1d8416ae0408d89"))
        self.assertIn("exam_reference", codes("617fa18efd53ff1629ed890f"))
        self.assertIn("figure_missing", codes("622df25ab70e4b50e44ff837"))

    def test_no_false_duplicates_on_decimals(self):
        # regression: stripping '.' once made "1.1 uA" equal "11 uA"
        for qid in ("63c7cce12981ed154ee92c94", "612151a6a34397610a9a2e58"):
            self.assertNotIn("option_duplicate", {s["code"] for s in question_signals(V[qid])})

    def test_assertion_stem_not_parsed_as_option_a(self):
        opts = parse_options(V["695ee00b88d7701e561aabc5"]["question"])
        self.assertTrue(opts["A"].startswith("Both A and R are true"))

    def test_comment_flags(self):
        rep = {r["reportId"]: r for r in REPORTS}
        flags = lambda rid: {s["code"] for s in comment_signals(rep[rid], V[rep[rid]["questionId"]])}
        self.assertIn("prompt_injection", flags("R027"))
        self.assertIn("gibberish", flags("R041"))
        self.assertIn("fixed_after_report", flags("R017"))
        self.assertEqual(flags("R003"), set())


class Results(unittest.TestCase):
    def test_results_file_matches_schema(self):
        rows = json.loads((__import__("pathlib").Path(__file__).resolve().parent.parent / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 44)
        for r in rows:
            self.assertIn(r["verdict"], TAX["verdicts"])
            self.assertIn(r["route"], ("auto_apply", "faculty_review", "no_change"))
            if r["verdict"] != "defect":
                self.assertIsNone(r["repair"]); self.assertIsNone(r["edit"]); self.assertEqual(r["route"], "no_change")
            else:
                self.assertIn(r["repair"], TAX["repairs"])
                if not TAX["repairs"][r["repair"]]["auto"]:
                    self.assertEqual(r["route"], "faculty_review"); self.assertIsNone(r["edit"])
            self.assertTrue(0 <= r["confidence"] <= 1)
            self.assertTrue(r["student_reply"] and r["evidence"])


if __name__ == "__main__":
    unittest.main()
