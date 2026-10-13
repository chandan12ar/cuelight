"""WCAG contrast of the design tokens, in both themes.

The palette is the one part of the design that can silently rot: a "small tweak" to a
grey makes secondary text unreadable and nothing fails. This reads the real tokens out
of style.css and holds every text pair to 4.5:1 and every non-text mark to 3:1.
"""

import os
import re
import unittest

CSS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "orchestra", "static", "style.css")

STATUSES = ("running", "completed", "failed", "stalled", "orphaned", "waiting")


def read_css():
    with open(CSS, encoding="utf-8") as fh:
        return fh.read()


def block(css, opener):
    start = css.index(opener)
    body = css[css.index("{", start) + 1:]
    depth, end = 1, 0
    for i, ch in enumerate(body):
        depth += (ch == "{") - (ch == "}")
        if depth == 0:
            end = i
            break
    return body[:end]


def tokens(text):
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\b", text))


def luminance(hex_color):
    h = hex_color.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class TestContrast(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        css = read_css()
        cls.light = tokens(block(css, "\n:root {"))
        dark = dict(cls.light)
        dark.update(tokens(block(css, ':root[data-theme="dark"]')))
        cls.dark = dark
        # The automatic dark theme must carry the same values as the explicit one.
        cls.auto_dark = tokens(block(css, ":root:not([data-theme=\"light\"])"))

    def each_theme(self):
        return (("light", self.light), ("dark", self.dark))

    def test_text_tokens_clear_4_5_on_every_surface_they_sit_on(self):
        for name, t in self.each_theme():
            for text in ("ink", "ink-2", "muted", "faint"):
                for surface in ("panel", "panel-2", "bg"):
                    ratio = contrast(t[text], t[surface])
                    self.assertGreaterEqual(ratio, 4.5, "{} {} on {}: {:.2f}".format(
                        name, text, surface, ratio))

    def test_status_colours_are_readable_as_text_on_the_panel(self):
        for name, t in self.each_theme():
            for status in STATUSES:
                ratio = contrast(t[status], t["panel"])
                self.assertGreaterEqual(ratio, 4.5, "{} {} text: {:.2f}".format(name, status, ratio))

    def test_labels_drawn_on_bars_are_readable(self):
        for name, t in self.each_theme():
            for status in ("completed", "failed", "orphaned", "waiting"):
                ratio = contrast(t["bar-ink"], t[status])
                self.assertGreaterEqual(ratio, 4.5, "{} bar label on {}: {:.2f}".format(
                    name, status, ratio))

    def test_status_marks_stand_out_from_the_panel_as_graphics(self):
        for name, t in self.each_theme():
            for status in STATUSES + ("unknown",):
                ratio = contrast(t[status], t["panel"])
                self.assertGreaterEqual(ratio, 3.0, "{} {} mark: {:.2f}".format(name, status, ratio))

    def rule_value(self, selector, prop):
        """The value a CSS rule gives a property (the first rule for exactly this selector), or None."""
        css = read_css()
        match = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
        found = re.search(r"(?<![-\w])" + prop + r":\s*([^;]+);", match.group(1) + ";") if match else None
        return found.group(1).strip() if found else None

    def resolve(self, value, theme):
        token = re.fullmatch(r"var\(--([a-z0-9-]+)\)", value)
        color = theme[token.group(1)] if token else value
        short = re.fullmatch(r"#([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])", color)
        return "#" + "".join(c * 2 for c in short.groups()) if short else color

    def test_the_failure_badge_text_is_readable_in_both_themes(self):
        # A red count on the Advice tab: in dark mode the failure colour is light, so white text on it is not.
        background = self.rule_value(".badge.badge-fail", "background")
        text = self.rule_value(".badge.badge-fail", "color") or self.rule_value(".badge", "color")
        self.assertIsNotNone(background)
        self.assertIsNotNone(text)
        for name, t in self.each_theme():
            ratio = contrast(self.resolve(text, t), self.resolve(background, t))
            self.assertGreaterEqual(ratio, 4.5, "{} failure badge {} on {}: {:.2f}".format(
                name, text, background, ratio))

    def test_borders_are_visible_against_the_page(self):
        for name, t in self.each_theme():
            self.assertGreaterEqual(contrast(t["line-strong"], t["panel"]), 1.4, name)

    def test_the_automatic_dark_theme_matches_the_explicit_one(self):
        for key, value in self.auto_dark.items():
            if key in self.dark:
                self.assertEqual(value.lower(), self.dark[key].lower(), key)


if __name__ == "__main__":
    unittest.main()
