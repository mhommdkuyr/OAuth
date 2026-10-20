"""Offline regression checks for the Ibb resumable-census pipeline."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import deep_scan
import merge_results
import enrich_web


class DeepCensusTests(unittest.TestCase):
    def test_grid_has_exact_tile_count_and_valid_parameters(self):
        bbox = [13.90, 14.00, 44.10, 44.20]
        for n in (10, 15, 20, 25, 30):
            tiles = deep_scan.grid_cells(bbox, n)
            self.assertEqual(len(tiles), n * n)
            self.assertTrue(all(int(t["radius"]) >= 1000 for t in tiles))
            self.assertTrue(all(11 <= int(t["zoom"]) <= 17 for t in tiles))
            self.assertTrue(all(-90 <= float(t["lat"]) <= 90 for t in tiles))

    def test_empty_serialized_json_is_not_data(self):
        self.assertFalse(merge_results.good("[]"))
        self.assertFalse(merge_results.good("{}"))
        self.assertFalse(merge_results.good('{"link":"","source":""}'))
        self.assertTrue(merge_results.good('{"name":"business owner"}'))
        self.assertTrue(merge_results.good('["https://example.com/photo.jpg"]'))

    def test_merge_joins_same_business_across_different_identifiers(self):
        rows = [
            {"name":"صيدلية التجربة", "place_id":"google-id-1", "phone":"+967 771 234 567",
             "address":"شارع تعز، إب", "latitude":"13.95001", "longitude":"44.18001",
             "source_engine":"mahanaicoach"},
            {"name":"صيدلية التجربة", "phone":"967771234567", "address":"شارع تعز، إب",
             "website":"https://example.ye", "source_engine":"gosom"},
            {"name":"متجر مستقل", "place_id":"google-id-2", "address":"شارع آخر، إب",
             "source_engine":"openstreetmap"},
        ]
        merged, duplicate_count, new_unique = merge_results.merge_records([], rows)
        self.assertEqual(len(merged), 2)
        self.assertEqual(duplicate_count, 1)
        self.assertEqual(new_unique, 2)
        first = next(r for r in merged if r["name"] == "صيدلية التجربة")
        self.assertEqual(first.get("website"), "https://example.ye")
        self.assertTrue("mahanaicoach" in first["source_engine"])
        self.assertTrue("gosom" in first["source_engine"])

    def test_openstreetmap_query_uses_bbox_and_named_features(self):
        query = deep_scan.osm_query([13.9, 14.0, 44.1, 44.2])
        self.assertIn('[out:json][timeout:90]', query)
        self.assertIn('["name"]["shop"]', query)
        self.assertIn('["name"]["amenity"]', query)
        self.assertIn("out center tags", query)

    def test_public_website_parser_finds_images_email_address_socials(self):
        html = """
        <html><head>
        <meta property="og:image" content="/images/storefront.jpg">
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"LocalBusiness","name":"Example Shop",
         "email":"contact@example.ye","telephone":"+967711234567",
         "address":{"streetAddress":"Taiz Street","addressLocality":"Ibb","addressCountry":"YE"},
         "image":"https://example.ye/images/front-door.jpg",
         "sameAs":["https://www.instagram.com/example_shop/"]}
        </script>
        </head><body><a href="https://www.facebook.com/example.shop">Facebook</a></body></html>
        """
        p = enrich_web.Parser()
        p.feed(html)
        data = enrich_web.extract("https://example.ye/", html, p)
        self.assertIn("contact@example.ye", data["website_emails"])
        self.assertIn("Taiz Street", data["website_address"])
        self.assertTrue(data["website_images"])
        self.assertIn("instagram.com/example_shop", data["instagram"])
        self.assertIn("facebook.com/example.shop", data["facebook"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
