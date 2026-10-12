"""Built-in list prices: the default when there is no price file of your own."""

import json
import os
import tempfile
import unittest
from unittest import mock

from orchestra import listprices
from orchestra.build import RunBuilder
from orchestra.pricing import PriceSource, default_source
from tests.fixtures import build_session

SEEN = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001", "claude-haiku-5-5", "claude-fable-5-1")


class TestTable(unittest.TestCase):
    def setUp(self):
        self.table = listprices.table()

    def test_every_model_seen_in_real_transcripts_has_a_price(self):
        for model in SEEN:
            self.assertIsNotNone(self.table.price_for(model), model)

    def test_a_model_gets_its_own_row_before_its_family(self):
        self.assertEqual(self.table.price_for("claude-fable-5-1")["cache_read"], 0.25)
        self.assertEqual(self.table.price_for("claude-fable-5")["cache_read"], 1.0)
        self.assertEqual(self.table.price_for("claude-opus-5-5")["input"], 4.0)
        self.assertEqual(self.table.price_for("claude-opus-5")["input"], 5.0)

    def test_cache_writes_use_the_hour_rate_so_cost_is_never_understated(self):
        for pattern, prices in self.table.models:
            self.assertEqual(prices["cache_create"], prices["input"] * 2, pattern)

    def test_models_not_in_the_table_stay_unpriced(self):
        for model in ("claude-opus-4-1", "claude-3-5-sonnet-20241022", "gpt-x", ""):
            self.assertIsNone(self.table.price_for(model), model)

    def test_it_is_labelled_with_its_date(self):
        self.assertEqual(self.table.source, "list")
        self.assertRegex(listprices.AS_OF, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(self.table.as_of, listprices.AS_OF)

    def test_the_cheaper_models_advice_suggests_are_priced(self):
        for family, (alias, model) in listprices.CHEAPER.items():
            self.assertIsNotNone(self.table.price_for(model), family)
            self.assertIn(alias, ("sonnet", "haiku"))


class TestFallback(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "prices.json")

    def test_no_file_falls_back_to_the_list(self):
        self.assertEqual(PriceSource(self.path, fallback=listprices.table()).get().source, "list")

    def test_your_file_wins(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"models": {"*": {"input": 1, "output": 1}}}, fh)
        table = PriceSource(self.path, fallback=listprices.table()).get()
        self.assertEqual((table.source, table.as_of), ("yours", ""))

    def test_a_broken_file_is_reported_not_replaced(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        src = PriceSource(self.path, fallback=listprices.table())
        self.assertIsNone(src.get())
        self.assertIn("unusable", src.error)

    def test_without_a_fallback_nothing_changes(self):
        self.assertIsNone(PriceSource(self.path).get())

    def test_default_source_uses_the_list_without_a_file(self):
        with mock.patch.dict(os.environ, {"ORCHESTRA_PRICES": self.path}):
            self.assertEqual(default_source().get().source, "list")

    def test_off_turns_cost_off(self):
        with mock.patch.dict(os.environ, {"ORCHESTRA_PRICES": "off"}):
            self.assertIsNone(default_source().get())

    def test_the_run_says_where_its_prices_came_from(self):
        paths = build_session(self.dir)
        run = RunBuilder(paths, prices=PriceSource(self.path, fallback=listprices.table())).refresh()
        self.assertTrue(run.cost["enabled"])
        self.assertEqual((run.cost["source"], run.cost["as_of"]), ("list", listprices.AS_OF))


if __name__ == "__main__":
    unittest.main()
