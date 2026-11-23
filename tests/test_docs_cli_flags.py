"""Every `--flag` the docs name exists in the code, and a command line that runs one tool uses that tool's flags.

    python -I tests/test_docs_cli_flags.py

`tests/test_docs_repo_paths.py` checks the paths the docs name; this checks the options. A renamed or removed
argparse option leaves `python -I tools/x.py --old` in a README or guide that no longer runs. Two checks:

1. a `--flag` anywhere in a Markdown page appears as a string in some tracked non-Markdown source file;
2. in a fenced command that starts `python [-I] tools/x.py` (or `scripts/`, or `make_game.py`), every flag appears in
   that script (a script that builds flags as `"--" + name` is skipped; `--help` is argparse's own).

Reads tracked files only; no game files.
"""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLAG = re.compile(r"(?<![\w-])(--[a-z][\w-]*)")
SOURCE_SUFFIXES = (".py", ".ps1", ".cpp", ".cc", ".c", ".h", ".hpp", ".cmake", ".txt", ".cs", ".cjs", ".js", ".html",
                   ".bat", ".sh", ".yml", ".yaml", ".csproj")
# Flags of other programs the docs quote: gh (AGENTS.md), pip (docs/shader_validation.md), argparse itself.
FOREIGN = {"--search", "--state", "--index-url", "--help"}
# Pages that quote another project's tree (SCARAB, americas-recomp).
OTHER_PROJECTS = {"docs/fssm_crosscheck.md", "docs/d3d12_xs_surface.md"}
COMMAND = re.compile(r"\bpython3?(?: -I)? ((?:tools|scripts)/[\w/]+\.py|make_game\.py)\b")


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [p for p in out.splitlines() if p]
    except (OSError, subprocess.CalledProcessError):
        found = []
        for root, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "build", "__pycache__", "node_modules")]
            found += [os.path.relpath(os.path.join(root, f), ROOT).replace(os.sep, "/") for f in files]
        return found


def read(path):
    with open(os.path.join(ROOT, path), encoding="utf-8", errors="replace") as f:
        return f.read()


def unknown_flags(pages, code):
    """[(page:line, flag)] for flags in `pages` ({name: text}) that do not occur in `code` (one string)."""
    out = []
    for page, text in sorted(pages.items()):
        for n, line in enumerate(text.split("\n"), 1):
            for flag in sorted(set(FLAG.findall(line))):
                if flag not in FOREIGN and flag not in code:
                    out.append((f"{page}:{n}", flag))
    return out


def fenced_commands(text):
    """[(line number, one command)] for the fenced code lines, with backslash/backtick continuations joined."""
    out, fence, buf, start = [], False, "", 0
    for n, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("```"):
            fence, buf = not fence, ""
            continue
        if not fence:
            continue
        if not buf:
            start = n
        stripped = line.rstrip()
        if stripped.endswith(("\\", "`")) and not stripped.endswith("``"):
            buf += " " + stripped[:-1]
            continue
        out.append((start, buf + " " + stripped))
        buf = ""
    return out


def tool_flag_mismatches(pages, sources):
    """[(page:line, tool, flag)] for fenced commands running one tool with a flag its source lacks.
    `sources` maps a tool path to its text."""
    out = []
    for page, text in sorted(pages.items()):
        for n, cmd in fenced_commands(text):
            m = COMMAND.search(cmd)
            if not m or m.group(1) not in sources:
                continue
            tool = m.group(1)
            if '"--" +' in sources[tool] or "'--' +" in sources[tool]:
                continue
            for flag in sorted(set(FLAG.findall(cmd))):
                if flag not in FOREIGN and flag not in sources[tool]:
                    out.append((f"{page}:{n}", tool, flag))
    return out


class DocsCliFlags(unittest.TestCase):
    def test_checkers_report_only_the_missing_flag(self):
        pages = {"docs/a.md": "Use `--real` or `--gone`, and `gh --search`.\n"
                              "```\npython -I tools/x.py --real --old \\\n    --more\npython -I tools/y.py --old\n```\n"}
        self.assertEqual(unknown_flags(pages, '"--real" "--more" "--old"'), [("docs/a.md:1", "--gone")])
        sources = {"tools/x.py": 'add("--real") add("--more")', "tools/y.py": 'add("--old")'}
        self.assertEqual(tool_flag_mismatches(pages, sources), [("docs/a.md:3", "tools/x.py", "--old")])
        sources["tools/x.py"] += ' "--" + name'
        self.assertEqual(tool_flag_mismatches(pages, sources), [])

    def test_docs_name_existing_flags(self):
        files = tracked_files()
        code = "\n".join(read(p) for p in files if p.endswith(SOURCE_SUFFIXES) and not p.startswith("docs/"))
        pages = {p: read(p) for p in files if p.endswith(".md") and p not in OTHER_PROJECTS}
        self.assertTrue(code and pages)
        missing = unknown_flags(pages, code)
        self.assertEqual(missing, [], "flags in the docs that no source file mentions: " +
                         "; ".join(f"{where} {flag}" for where, flag in missing))

    def test_fenced_commands_use_the_flags_of_their_tool(self):
        files = tracked_files()
        sources = {p: read(p) for p in files if p.endswith(".py") and (p.startswith(("tools/", "scripts/")) or p == "make_game.py")}
        pages = {p: read(p) for p in files if p.endswith(".md") and p not in OTHER_PROJECTS}
        bad = tool_flag_mismatches(pages, sources)
        self.assertEqual(bad, [], "fenced commands using a flag their script lacks: " +
                         "; ".join(f"{where} {tool} {flag}" for where, tool, flag in bad))


if __name__ == "__main__":
    unittest.main()
