"""Build DiscordAnt for Windows.

    python build.py               the app folder:   dist/DiscordAnt/
    python build.py --installer   ...and the installer:
                                  release/DiscordAnt-Setup-<version>.exe

What it does:

  1. PyInstaller makes a one-folder build of app/gui.py.
  2. The Chromium that this version of Playwright drives is copied into the
     build, so the app runs on a PC that has nothing else set up.
  3. The build is started once with --selftest in a made-up empty user
     folder. That proves it does not lean on anything installed on this PC.
  4. --installer: Inno Setup packs the folder into one setup program.

Needs: pip install -r requirements-build.txt
       Inno Setup 6 for --installer (https://jrsoftware.org/isinfo.php)

Nothing here touches an installed copy of the app or its data.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
APP = BASE / "app"
DIST = BASE / "dist"
WORK = BASE / "build"
RELEASE = BASE / "release"

# The app's own modules. gui.py imports them inside a try/except, which
# PyInstaller's import scan can miss, so each one is named here.
MODULES = ("appinfo", "keep_awake", "scraper_core", "store", "jobs", "user_history",
           "exports", "persistence")

VERSION_INFO = """\
VSVersionInfo(
  ffi=FixedFileInfo(filevers=({a}, {b}, {c}, 0), prodvers=({a}, {b}, {c}, 0),
                    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', '{name}'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', '{name}'),
      StringStruct('OriginalFilename', '{name}.exe'),
      StringStruct('ProductName', '{name}'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def app_info():
    text = (APP / "appinfo.py").read_text(encoding="utf-8")
    name = re.search(r'^APP_NAME\s*=\s*"([^"]+)"', text, re.M).group(1)
    version = re.search(r'^__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text, re.M)
    return name, ".".join(version.groups()), version.groups()


def build_app(name, version, parts):
    for p in (WORK, DIST / name):
        if p.exists():
            shutil.rmtree(p)
    WORK.mkdir(parents=True)
    a, b, c = parts
    version_file = WORK / "version_info.txt"
    version_file.write_text(VERSION_INFO.format(a=a, b=b, c=c, v=version, name=name),
                            encoding="utf-8")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", name,
        "--windowed",                     # no console window
        "--paths", str(APP),
        "--collect-all", "playwright",    # the driver and its package data
        "--version-file", str(version_file),
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(WORK),
    ]
    # app/icon.ico is the exe's icon and is also shipped inside the build, so
    # the window can show it.
    icon = APP / "icon.ico"
    if icon.exists():
        cmd += ["--icon", str(icon), "--add-data", f"{icon}{os.pathsep}."]
    for m in MODULES:
        cmd += ["--hidden-import", m]
    cmd.append(str(APP / "gui.py"))

    print("Running:", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)
    app_dir = DIST / name
    if not (app_dir / f"{name}.exe").exists():
        sys.exit("PyInstaller finished but made no exe.")
    for extra in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(BASE / extra, app_dir / extra)
    return app_dir


def browser_cache():
    """Where `playwright install` keeps browsers on this machine."""
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if custom and custom != "0":
        return Path(custom)
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def bundle_browser(app_dir):
    """Copy Chromium into the build, as a "browser" folder beside the exe.
    gui.py points Playwright there. It sits that high up on purpose: Windows
    refuses paths longer than 260 characters, and the browser's own folders
    run deep."""
    listing = next(app_dir.rglob("driver/package/browsers.json"))
    wanted = {b["name"]: b["revision"]
              for b in json.loads(listing.read_text())["browsers"]}
    names = [f"chromium-{wanted['chromium']}"]
    if sys.platform == "win32":
        # A small helper Playwright uses to check Windows has what the browser needs.
        names.append(f"winldd-{wanted['winldd']}")

    cache = browser_cache()
    if any(not (cache / n / "INSTALLATION_COMPLETE").exists() for n in names):
        print("Downloading Chromium (one time)...", flush=True)
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                       check=True)

    dest = app_dir / "browser"
    total = 0
    for n in names:
        # DEPENDENCIES_VALIDATED is this PC's "already checked" mark. Leaving it
        # out makes the app do that check itself the first time it runs.
        shutil.copytree(cache / n, dest / n,
                        ignore=shutil.ignore_patterns("DEPENDENCIES_VALIDATED"))
        total += sum(f.stat().st_size for f in (dest / n).rglob("*") if f.is_file())
    print(f"Bundled {', '.join(names)} ({total / 1_048_576:.0f} MB)", flush=True)


def selftest(app_dir, name):
    """Run the build's own self-test as if on a PC that has never seen it."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as fresh:
        env = dict(os.environ, LOCALAPPDATA=fresh)
        env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        subprocess.run([str(app_dir / f"{name}.exe"), "--selftest"], env=env,
                       timeout=300, check=False)
        result = Path(fresh) / name / "selftest_result.txt"
        text = result.read_text(encoding="utf-8") if result.exists() else "(no result file)"
    print("--- self-test ---\n" + text + "\n-----------------", flush=True)
    if "RESULT: PASS" not in text:
        sys.exit("SELF-TEST FAILED: the build is not usable.")
    if str(app_dir / "browser").lower() not in text.lower():
        sys.exit("SELF-TEST used a browser from outside the build.")


def find_iscc():
    found = shutil.which("iscc")
    if found:
        return found
    for root in (os.environ.get("LOCALAPPDATA", "") + r"\Programs",
                 os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("ProgramFiles", "")):
        exe = Path(root) / "Inno Setup 6" / "ISCC.exe"
        if root and exe.exists():
            return str(exe)
    return None


def build_installer(version):
    iscc = find_iscc()
    if not iscc:
        sys.exit("Inno Setup 6 was not found. Get it from "
                 "https://jrsoftware.org/isinfo.php and run this again.")
    RELEASE.mkdir(exist_ok=True)
    subprocess.run([iscc, f"/DAppVersion={version}", "/Qp",
                    str(BASE / "installer" / "DiscordAnt.iss")], check=True)
    for f in sorted(RELEASE.glob(f"*{version}*.exe")):
        print(f"Installer: {f} ({f.stat().st_size / 1_048_576:.0f} MB)")


def main():
    name, version, parts = app_info()
    print(f"Building {name} {version}", flush=True)
    app_dir = build_app(name, version, parts)
    bundle_browser(app_dir)
    selftest(app_dir, name)
    print(f"App folder: {app_dir}")
    if "--installer" in sys.argv:
        build_installer(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
