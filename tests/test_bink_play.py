"""tools/bink_play.py: name matching and the name-table cache. Synthetic names only, no game files."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bink_play  # noqa: E402

NAMES = [
    ("Stinger/INTRO_A.bk2", 4096, "3840", "2160", "450"),
    ("Stinger/INTRO_B.bk2", 8192, "3840", "2160", "300"),
    ("TV/News_01.bk2", 12288, "1280", "720", "40"),
]


class PickTest(unittest.TestCase):
    def test_exact_path_wins_case_insensitively_and_with_backslashes(self):
        hit, _ = bink_play.pick(NAMES, "stinger\\intro_a.BK2")
        self.assertEqual(hit[1], 4096)

    def test_a_unique_part_of_the_name_is_enough(self):
        hit, _ = bink_play.pick(NAMES, "news")
        self.assertEqual(hit[0], "TV/News_01.bk2")

    def test_an_ambiguous_part_returns_the_candidates(self):
        hit, candidates = bink_play.pick(NAMES, "intro")
        self.assertIsNone(hit)
        self.assertEqual([c[0] for c in candidates], ["Stinger/INTRO_A.bk2", "Stinger/INTRO_B.bk2"])

    def test_no_match(self):
        self.assertEqual(bink_play.pick(NAMES, "credits"), (None, []))


class CacheTest(unittest.TestCase):
    def test_the_table_is_named_once_and_rebuilt_when_the_archive_changes(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            archive, mount = d / "movies.rpf", d / "common.rpf"
            archive.write_bytes(b"x" * 16)
            mount.write_bytes(b"y")
            calls = []

            def fake_names(argv):
                calls.append(argv)
                out = Path(argv[argv.index("--out") + 1])
                out.write_text("source,offset,size,path,confidence,width,height,frames\n"
                               "movies.rpf,4096,10,Stinger/INTRO_A.bk2,anchored,3840,2160,450\n", encoding="utf-8")
                return 0

            with mock.patch.object(bink_play, "cache_path", return_value=d / "cache" / "bink_names.csv"), \
                    mock.patch.object(bink_play.bink_names, "main", side_effect=fake_names):
                first = bink_play.load_names(archive, mount)
                second = bink_play.load_names(archive, mount)
                self.assertEqual(len(calls), 1, "the second call reads the cache")
                self.assertEqual(first, second)
                self.assertEqual(first[0][:2], ("Stinger/INTRO_A.bk2", 4096))
                archive.write_bytes(b"x" * 32)  # a different archive size: the cache is stale
                bink_play.load_names(archive, mount)
                self.assertEqual(len(calls), 2)
                bink_play.load_names(archive, mount, use_cache=False)
                self.assertEqual(len(calls), 3, "--no-cache names again")


if __name__ == "__main__":
    unittest.main()
