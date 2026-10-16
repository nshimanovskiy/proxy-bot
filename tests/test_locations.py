"""Названия локаций: внутренние имена нод Steel Proxy → «🇩🇪 Германия»."""
import unittest

from bot import locations as L


class LocationNamesTest(unittest.TestCase):
    def test_node_names_become_countries(self):
        cases = {
            ("DE", "DE_ISP01"): "🇩🇪 Германия · ISP01",
            ("KZ_DC02", "KZ_DC02"): "🇰🇿 Казахстан · DC02",
            ("", "NL_DC02"): "🇳🇱 Нидерланды · DC02",
            ("US", "США_DC05"): "🇺🇸 США · DC05",
            ("USA", "США_DC05"): "🇺🇸 США · DC05",
            ("RU", "Россия"): "🇷🇺 Россия",
            ("TR", "Турция"): "🇹🇷 Турция",
            ("ru", "Россия"): "🇷🇺 Россия",
            ("DЕ", "DE_ISP01"): "🇩🇪 Германия · ISP01",   # кириллическая «Е» в коде
        }
        for (cc, title), expected in cases.items():
            self.assertEqual(L.display(cc, title), expected, (cc, title))

    def test_list_hides_suffix_unless_country_repeats(self):
        names = L.display_many([("DE", "DE_ISP01"), ("DE", "DE_DC03"),
                                ("US", "США_DC05"), ("FI", "Финляндия")])
        self.assertEqual(names, ["🇩🇪 Германия · ISP01", "🇩🇪 Германия · DC03",
                                 "🇺🇸 США", "🇫🇮 Финляндия"])

    def test_unknown_stays_readable(self):
        self.assertEqual(L.display("XX", "Some Node"), "🌐 Some Node")
        self.assertEqual(L.display(None, "Луна-1"), "🌐 Луна-1")
        self.assertEqual(L.display(None, None), "🌐 —")


if __name__ == "__main__":
    unittest.main()
