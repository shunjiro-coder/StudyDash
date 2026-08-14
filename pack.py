"""Build a distributable StudyDash zip that provably contains none of your data.

The safety comes from the source, not from a filter: the archive is built from
`git archive`, which emits ONLY tracked files. study.db, uploads/, backups/,
credentials.json and settings.local.json are all git-ignored, so they cannot be
tracked, so they cannot end up in the zip. The scan afterwards is a second lock on
the same door — if it ever fires, something is tracked that should not be.

    python3 pack.py            -> dist/StudyDash-v<version>.zip
    python3 pack.py --check    -> audit only, build nothing
"""

import os
import subprocess
import sys
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(BASE_DIR, "dist")

# Anything matching these must never appear in a pack. Substring match on the
# archive's internal paths, which use forward slashes on every platform.
FORBIDDEN = (
    "study.db",           # the owner's whole study history
    "uploads/",           # their photographed textbooks and worksheets
    "backups/",           # DB snapshots
    "credentials.json",   # Google OAuth client
    "token.json", "token.pickle",
    "settings.local.json",  # may carry machine-local paths
    "studydash.log",
    ".env",
)

# Present-but-empty is fine; these are the files a recipient truly needs.
REQUIRED = (
    "app.py", "requirements.txt", "VERSION", "doctor.py",
    "Start-Mac.command", "Start-Windows.bat",
    "templates/index.html", "static/app.js", "prompts/stem.txt",
)


def _git(*args):
    return subprocess.run(["git", *args], cwd=BASE_DIR, capture_output=True,
                          text=True, check=True).stdout


def tracked_files():
    return [p for p in _git("ls-files").splitlines() if p]


def audit(names):
    """Returns (problems, missing). Empty both = safe to ship."""
    problems = [n for n in names if any(bad in n for bad in FORBIDDEN)]
    missing = [r for r in REQUIRED if r not in names]
    return problems, missing


def version():
    try:
        with open(os.path.join(BASE_DIR, "VERSION"), encoding="utf-8") as fh:
            return fh.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def build():
    names = tracked_files()
    problems, missing = audit(names)
    if problems:
        print("REFUSING TO PACK — these tracked files look like private data:")
        for p in problems:
            print("   ", p)
        print("\nUntrack them first:  git rm --cached <path>")
        return None
    if missing:
        print("REFUSING TO PACK — required files are not tracked:")
        for m in missing:
            print("   ", m)
        return None

    os.makedirs(DIST_DIR, exist_ok=True)
    out = os.path.join(DIST_DIR, "StudyDash-v%s.zip" % version())
    # Written by git, so the contents are exactly the tracked tree at HEAD.
    with open(out, "wb") as fh:
        fh.write(subprocess.run(
            ["git", "archive", "--format=zip", "-9",
             "--prefix=StudyDash/", "HEAD"],
            cwd=BASE_DIR, capture_output=True, check=True).stdout)

    # Re-audit what actually landed in the zip, not what we believed we put there.
    with zipfile.ZipFile(out) as z:
        inside = [n[len("StudyDash/"):] for n in z.namelist()
                  if n.startswith("StudyDash/")]
    problems, missing = audit(inside)
    if problems or missing:
        os.remove(out)
        print("BUILT ZIP FAILED ITS OWN AUDIT — deleted.")
        for p in problems:
            print("    private:", p)
        for m in missing:
            print("    missing:", m)
        return None

    mb = os.path.getsize(out) / (1024 * 1024)
    print("  packed %d files -> %s (%.1f MB)" % (len(inside), out, mb))
    print("  audit: no private data, all required files present")
    return out


def main():
    if "--check" in sys.argv:
        names = tracked_files()
        problems, missing = audit(names)
        print("  tracked files: %d" % len(names))
        print("  private-looking: %s" % (problems or "none"))
        print("  required missing: %s" % (missing or "none"))
        return 1 if (problems or missing) else 0
    return 0 if build() else 1


if __name__ == "__main__":
    sys.exit(main())
