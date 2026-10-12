"""Pure result-accounting regressions; no app imports, data files or network."""

from collections import UserDict
import unittest

from core.send_results import contact_names, completed_count, resolved_contact_names


class SendResultAccountingTests(unittest.TestCase):
    def test_one_string_success_and_eight_failures_complete_nine_contacts(self):
        result = {
            "ok": ["synthetic-success"],
            "failed": [{"name": f"synthetic-failure-{index}"} for index in range(8)],
            "deferred": [{"name": "synthetic-not-attempted"}],
        }
        self.assertEqual(completed_count(result), 9)

    def test_successful_contact_is_not_deferred_again_after_system_failure(self):
        result = {"ok": ["synthetic-success"],
                  "failed": [{"name": "_system", "reason": "synthetic browser stopped"}]}
        active = ["synthetic-success", "synthetic-remaining"]
        resolved = resolved_contact_names(result)
        deferred = [name for name in active if name not in resolved]
        self.assertEqual(deferred, ["synthetic-remaining"])
        self.assertEqual(completed_count(result), 1)

    def test_current_and_historical_entry_shapes_are_supported_together(self):
        result = {
            "ok": ["synthetic-a", {"name": "synthetic-b"}],
            "failed": ["synthetic-c", {"name": "synthetic-d", "reason": "ignored"}],
            "deferred": ["synthetic-e", {"name": "synthetic-f"}],
        }
        self.assertEqual(completed_count(result), 4)
        self.assertEqual(resolved_contact_names(result), {
            "synthetic-a", "synthetic-b", "synthetic-c", "synthetic-d", "synthetic-e", "synthetic-f",
        })

    def test_real_skipped_contacts_count_but_deferred_and_system_entries_do_not(self):
        result = {
            "ok": ["synthetic-a"],
            "failed": [{"name": "_system", "reason": "not a contact"}],
            "skipped": [{"name": "synthetic-b", "reason": "unsupported route"},
                        "synthetic-c", {"name": "_system", "reason": "budget expired"}],
            "deferred": [{"name": "synthetic-d"}],
        }
        self.assertEqual(completed_count(result), 3)
        self.assertEqual(resolved_contact_names(result), {
            "synthetic-a", "synthetic-b", "synthetic-c", "synthetic-d",
        })

    def test_skipped_contact_is_not_deferred_after_later_driver_failure(self):
        result = {
            "ok": ["synthetic-success"],
            "skipped": [{"name": "synthetic-unsupported", "reason": "first message not allowed"},
                        {"name": "_system", "reason": "budget explanation"}],
            "failed": [{"name": "_system", "reason": "synthetic browser stopped"}],
        }
        active = ["synthetic-success", "synthetic-unsupported", "synthetic-remaining"]
        resolved = resolved_contact_names(result)
        self.assertEqual([name for name in active if name not in resolved], ["synthetic-remaining"])
        self.assertNotIn("_system", resolved)
        self.assertEqual(completed_count(result), 2)

    def test_contact_is_counted_once_across_duplicate_outcomes(self):
        result = {"ok": ["synthetic-a", "synthetic-a"],
                  "failed": [{"name": "synthetic-a"}, {"name": "synthetic-b"}],
                  "skipped": ["synthetic-b"], "deferred": ["synthetic-a"]}
        self.assertEqual(completed_count(result), 2)
        self.assertEqual(resolved_contact_names(result), {"synthetic-a", "synthetic-b"})

    def test_blank_system_and_invalid_entry_types_are_ignored(self):
        entries = [None, False, True, 0, 3.5, b"synthetic-bytes", [], ["nested"],
                   "", " \t\n\u00a0", "_system", {"name": "_system"}, {"name": " "},
                   {"name": None}, {"name": 12}, {"name": ["nested"]}, {"name": {"nested": 1}},
                   {"reason": "synthetic-not-a-name"}, {"display_name": "synthetic-not-an-identity"}]
        self.assertEqual(contact_names(entries), set())

    def test_scalar_outcome_fields_are_not_iterated_as_contact_names(self):
        for invalid in (None, 0, True, "synthetic-single-string", b"bytes", {"name": "synthetic-single-map"}):
            with self.subTest(invalid=invalid):
                self.assertEqual(contact_names(invalid), set())
                self.assertEqual(completed_count({"ok": invalid, "failed": invalid, "skipped": invalid}), 0)

    def test_invalid_results_and_missing_fields_are_empty(self):
        for invalid in (None, False, 0, "synthetic", [], {}):
            with self.subTest(invalid=invalid):
                self.assertEqual(completed_count(invalid), 0)
                self.assertEqual(resolved_contact_names(invalid), set())

    def test_original_identity_preserves_spaces_nbsp_and_unicode(self):
        names = [" synthetic-a ", "\u00a0synthetic-b\u00a0", " synthetic 用户 ", "synthetic-a"]
        self.assertEqual(contact_names(names + [{"name": names[1]}]), set(names))
        result = {"ok": names[:3], "failed": [{"name": "_system"}]}
        active = names[:3] + ["synthetic-remaining"]
        self.assertEqual([name for name in active if name not in resolved_contact_names(result)],
                         ["synthetic-remaining"])

    def test_only_exact_system_marker_is_reserved_without_renaming_other_keys(self):
        self.assertEqual(contact_names(["_system", " _system ", "_SYSTEM", {"name": " _system "}]),
                         {" _system ", "_SYSTEM"})

    def test_mapping_entries_and_generator_collections_are_supported(self):
        entries = (item for item in ("synthetic-a", UserDict(name="synthetic-b")))
        self.assertEqual(contact_names(entries), {"synthetic-a", "synthetic-b"})

    def test_accounting_does_not_mutate_source_result(self):
        result = {"ok": ["synthetic-a"], "failed": [{"name": "synthetic-b", "reason": "keep"}],
                  "deferred": [{"name": "synthetic-c"}]}
        expected = repr(result)
        completed_count(result)
        resolved_contact_names(result)
        self.assertEqual(repr(result), expected)


if __name__ == "__main__":
    unittest.main()
