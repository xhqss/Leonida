"""tools/exe_checklist.py: the ASSUMPTION / UNVERIFIED / VERIFY notes, gathered for the executable's first look.

The repository here is a small tree built in a temporary folder (one shim, one doc), so the counts are exact; the
last test runs the tool on this repository to make sure every note it finds has a file, a line and some text.
Standard library only; no game files.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import exe_checklist as ec  # noqa: E402

SHIM = """// demo: a shim with two notes.
//
// ASSUMPTION: the quota is 1 GiB until the executable
// shows its own.
#include <x>
constexpr GUID kDemoClass = {0x704c3f58, 0xe629, 0x4cc2, {0xb1, 0x97, 0x30, 0x51, 0x1b, 0x99, 0x6f, 0xe2}};
constexpr size_t kSize = 18;   // UNVERIFIED, see the header
// The SKU. UNVERIFIED: nothing
// compares it.
int f();
"""
OTHER = """// no class here
// ASSUMPTION: a note in a file that serves no runtime class.
"""
DOC = """# Demo

Some text.

- **Names:** fine.
- **VERIFY:** the container names, from the
  executable's calls.

**ASSUMPTION:** a paragraph note
that spans two lines.
"""
SAVE_CLASS = "704c3f58-e629-4cc2-b197-30511b996fe2"


class ExeChecklist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "runtime", "shims"))
        os.makedirs(os.path.join(self.root, "docs"))
        for rel, text in (("runtime/shims/demo.cpp", SHIM), ("runtime/shims/other.cpp", OTHER), ("docs/demo.md", DOC)):
            with open(os.path.join(self.root, rel), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)

    def tearDown(self):
        self.tmp.cleanup()

    def test_notes_start_at_their_marker_and_skip_trailing_comments(self):
        notes = ec.collect(self.root)
        demo = [n for n in notes if n["file"] == "runtime/shims/demo.cpp"]
        self.assertEqual([(n["line"], n["kind"]) for n in demo], [(3, "ASSUMPTION"), (8, "UNVERIFIED")])
        self.assertEqual(demo[0]["text"], "the quota is 1 GiB until the executable shows its own.")
        self.assertEqual(demo[1]["text"], "nothing compares it.")
        self.assertEqual(demo[0]["classes"], [SAVE_CLASS])

    def test_doc_notes_are_the_list_item_or_paragraph(self):
        docs = [n for n in ec.collect(self.root) if n["file"] == "docs/demo.md"]
        self.assertEqual([(n["line"], n["kind"]) for n in docs], [(6, "VERIFY"), (9, "ASSUMPTION")])
        self.assertEqual(docs[0]["text"], "the container names, from the executable's calls.")
        self.assertEqual(docs[1]["text"], "a paragraph note that spans two lines.")

    def test_called_notes_come_first(self):
        report = os.path.join(self.root, "x.gdk_calls.json")
        with open(report, "w", encoding="utf-8") as f:
            json.dump({"images": [{"classes": [{"class_id": SAVE_CLASS.upper()}]}]}, f)
        out = os.path.join(self.root, "checklist.md")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ec.main(["--root", self.root, "--gdk-calls", report, "--out", out]), 0)
        with open(out, "rb") as f:
            raw = f.read()
        self.assertNotIn(b"\r\n", raw)
        lines = [l for l in raw.decode("utf-8").split("\n") if l.startswith("- [ ]")]
        self.assertIn("demo.cpp:3` ASSUMPTION **called**", lines[0])
        self.assertIn("demo.cpp:8` UNVERIFIED **called**", lines[1])
        self.assertIn("other.cpp:2` ASSUMPTION:", lines[2])   # serves no class: no tag, after the called ones
        self.assertEqual(len(lines), 5)

    def test_a_class_the_executable_does_not_call(self):
        report = os.path.join(self.root, "y.json")
        with open(report, "w", encoding="utf-8") as f:
            json.dump({"classes": [{"class_id": "00000000-0000-0000-0000-000000000001"}]}, f)   # one image, no "images"
        text = ec.render(ec.collect(self.root), ec.called_classes([report]))
        self.assertIn("demo.cpp:3` ASSUMPTION (not called)", text)

    def test_an_unreadable_report_is_exit_1(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(ec.main(["--root", self.root, "--gdk-calls", os.path.join(self.root, "missing.json")]), 1)
        self.assertIn("cannot read", stderr.getvalue())

    def test_this_repository(self):
        notes = ec.collect(ROOT)
        self.assertTrue(notes)
        for n in notes:
            self.assertTrue(n["text"].strip(), n)
            self.assertGreater(n["line"], 0)
        # the store shim's unverified sizes are on the list, from the source and from the docs
        self.assertTrue(any(n["file"] == "runtime/shims/xgameruntime_store.cpp" and "STORE_SKU_ID_SIZE" in n["text"]
                            for n in notes))
        self.assertTrue(any(n["file"] == "docs/runtime_imports.md" and n["kind"] == "UNVERIFIED" for n in notes))


if __name__ == "__main__":
    unittest.main()
