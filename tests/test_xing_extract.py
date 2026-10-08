import json
import unittest

from bs4 import BeautifulSoup

from source_records import EMPTY, MALFORMED, OK
from xing_extract import (LOW_QUALITY, assess_xing, clean_body, decode_response, header_location,
                          parse_jobposting, repair_mojibake)

BODY = ("Aufgaben: Entwicklung von Embedded Software in C++ für Steuergeräte nach ASPICE. "
        "Profil: Studium der Elektrotechnik, Erfahrung mit MATLAB/Simulink und AUTOSAR. ") * 2


def garble(text):
    return text.encode("utf-8").decode("latin-1")


class DecodeTests(unittest.TestCase):
    def test_utf8_without_charset(self):
        self.assertEqual(decode_response("Größe für".encode("utf-8")), "Größe für")

    def test_false_latin1_claim_on_utf8_bytes(self):
        out = decode_response("Größe für".encode("utf-8"), "text/html; charset=ISO-8859-1")
        self.assertEqual(out, "Größe für")

    def test_real_latin1_is_respected(self):
        out = decode_response("für".encode("latin-1"), "text/html; charset=ISO-8859-1")
        self.assertEqual(out, "für")

    def test_meta_charset_and_fallback(self):
        raw = b'<html><meta charset="utf-8">Gr\xc3\xbc\xc3\x9fe</html>'
        self.assertIn("Grüße", decode_response(raw))
        self.assertEqual(decode_response(b"fur \xfc"), "fur ü")  # invalid utf-8 -> cp1252


class MojibakeTests(unittest.TestCase):
    def test_repair_roundtrip(self):
        self.assertEqual(repair_mojibake(garble("Steuergeräte für Größe")), "Steuergeräte für Größe")

    def test_clean_text_untouched(self):
        self.assertEqual(repair_mojibake("Steuergeräte für Größe, Ã alone"), "Steuergeräte für Größe, Ã alone")


class JsonLdTests(unittest.TestCase):
    def test_graph_jobposting(self):
        data = {"@graph": [{"@type": "WebSite"}, {
            "@type": "JobPosting", "title": "Embedded Engineer",
            "hiringOrganization": {"@type": "Organization", "name": "Acme GmbH"},
            "jobLocation": {"address": {"addressLocality": "Ingolstadt", "addressRegion": "Bayern"}},
            "description": "<p>Aufgaben:</p><ul><li>C++</li></ul>"}]}
        html = f'<script type="application/ld+json">{json.dumps(data)}</script>'
        got = parse_jobposting(BeautifulSoup(html, "lxml"))
        self.assertEqual(got["company"], "Acme GmbH")
        self.assertEqual(got["location"], "Ingolstadt, Bayern")
        self.assertEqual(got["description"], "Aufgaben: C++")

    def test_missing_or_invalid(self):
        self.assertIsNone(parse_jobposting(BeautifulSoup("<p>none</p>", "lxml")))
        bad = '<script type="application/ld+json">{oops</script>'
        self.assertIsNone(parse_jobposting(BeautifulSoup(bad, "lxml")))


class CleanBodyTests(unittest.TestCase):
    PAGE = ("Embedded Engineer Embedded Engineer Acme Kerpen Kerpen + 0 more Full-time Create search alert "
            "About this job " + BODY + " Similar jobs Other Job Jobs "
            "Embedded Jobs in München Software Jobs in Berlin Test Jobs in Hamburg")

    def test_keeps_body_only(self):
        out = clean_body(self.PAGE)
        self.assertTrue(out.startswith("Aufgaben"))
        self.assertNotIn("Similar jobs", out)
        self.assertNotIn("Jobs in", out)
        self.assertNotIn("Create search alert", out)

    def test_seo_tail_cut_without_similar_marker(self):
        page = "About this job " + BODY + " Embedded Jobs in München Software Jobs in Berlin Test Jobs in Hamburg"
        self.assertNotIn("Jobs in", clean_body(page))

    def test_repairs_mojibake_first(self):
        self.assertIn("für", clean_body("About this job " + garble(BODY)))


class AssessTests(unittest.TestCase):
    def test_statuses(self):
        self.assertEqual(assess_xing("", "T")[0], EMPTY)
        self.assertEqual(assess_xing(garble(BODY), "T"), (MALFORMED, "mojibake"))
        self.assertEqual(assess_xing(BODY, "Embedded Engineer"), (OK, ""))

    def test_low_quality_flags(self):
        status, flags = assess_xing(BODY + " Similar jobs", "Embedded Engineer")
        self.assertEqual(status, LOW_QUALITY)
        self.assertIn("page_furniture", flags)
        rep = ("Embedded Engineer " + BODY) * 3
        self.assertIn("repeated_title", assess_xing(rep, "Embedded Engineer")[1])


class HeaderLocationTests(unittest.TestCase):
    def test_city(self):
        self.assertEqual(header_location("Senior Engineer Acme Kerpen Kerpen + 0 more Full-time"), "Kerpen")

    def test_none(self):
        self.assertEqual(header_location("Senior Engineer Acme Full-time"), "")


if __name__ == "__main__":
    unittest.main()
