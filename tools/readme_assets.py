"""Rebuild the README's pictures and sample page from a real `kimeru try` run (nothing is drawn by hand).

    python tools/readme_assets.py

writes docs/sample/try-report.html, assets/readme/try-report.png (1100 px wide), assets/readme/try-report-mobile.png (the same
page in a 375 px wide frame, full length), assets/readme/social-preview.png (hero.svg on a 1280x640 card) and
assets/readme/sources.json (the sha256 of the sample page and hero.svg the pictures were made from: tests/test_readme_samples.py
fails when either changed without the pictures being made again).

The run happens in a fresh temporary folder with no KIMERU_* setting, so the page shows `out\\try` and no path of this PC.
The pictures are taken by a headless Chrome or Edge (the first that writes one). A headless window cannot be narrower than about
500 px, so the phone-wide picture is the page inside a 375 px iframe, cut to the frame with Pillow (pip install pillow; without
it nothing is written and the exit code is 1).
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets" / "readme"
BROWSERS = (r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "/usr/bin/google-chrome", "/usr/bin/chromium")
PHONE, TALL = 375, 7000
CARD = """<!doctype html><meta charset="utf-8"><style>html,body{margin:0;width:1280px;height:640px;overflow:hidden;
background:linear-gradient(135deg,#0e1726,#15243a)}img{position:absolute;left:40px;top:110px;width:1200px}</style>
<img src="hero.svg" alt="">"""
FRAME = """<!doctype html><meta charset="utf-8"><style>html,body{margin:0;background:#fff}
iframe{display:block;border:0;width:%dpx;height:%dpx}</style><iframe src="try-report.html"></iframe>""" % (PHONE, TALL)


def digest(path):
    """sha256 of a text file with its line ends made LF (a Windows checkout has CRLF, the Linux CI has LF)."""
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def main():
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("Pillow is needed for the phone-wide picture: pip install pillow (nothing was written)")
        return 1
    with tempfile.TemporaryDirectory() as d:
        env = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        env["PYTHONPATH"] = str(ROOT)
        subprocess.run([sys.executable, "-m", "kimeru", "try"], cwd=d, env=env, check=True, stdout=subprocess.DEVNULL)
        sample = ROOT / "docs" / "sample" / "try-report.html"
        sample.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(d) / "out" / "try" / "report.html", sample)
        print(sample)
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        shutil.copyfile(sample, work / "try-report.html")
        shutil.copyfile(ASSETS / "hero.svg", work / "hero.svg")
        (work / "card.html").write_text(CARD, encoding="utf-8")
        (work / "frame.html").write_text(FRAME, encoding="utf-8")
        shots = [("try-report.png", work / "try-report.html", 1100, 1660, None),
                 ("social-preview.png", work / "card.html", 1280, 640, None),
                 ("try-report-mobile.png", work / "frame.html", 520, TALL, _phone)]
        for name, page, w, h, finish in shots:
            png = work / name
            if not any(_shoot(b, page, png, w, h) for b in BROWSERS if Path(b).is_file()):
                print("no headless browser could take the picture: the pictures were not taken")
                return 1
            if finish and not finish(png):
                print(f"{name}: the page is longer than the {TALL} px frame; make TALL larger (nothing more was written)")
                return 1
            shutil.copyfile(png, ASSETS / name)
            print(ASSETS / name)
    sources = {"docs/sample/try-report.html": digest(sample), "assets/readme/hero.svg": digest(ASSETS / "hero.svg")}
    (ASSETS / "sources.json").write_text(json.dumps(sources, indent=1) + "\n", encoding="utf-8")
    print(ASSETS / "sources.json")
    return 0


def _shoot(browser, page, png, w, h):
    """One picture; False when this browser wrote none (a managed Edge may refuse headless runs)."""
    with tempfile.TemporaryDirectory() as tmp:   # an ASCII-only temporary path: some browsers fail on others
        out = Path(tmp) / "shot.png"
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                        "--blink-settings=preferredColorScheme=1", "--allow-file-access-from-files",
                        f"--user-data-dir={Path(tmp) / 'profile'}", f"--window-size={w},{h}", f"--screenshot={out}", page.as_uri()],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        if not out.is_file():
            return False
        shutil.copyfile(out, png)
        return True


def _phone(png):
    """Cut the picture to the 375 px frame and drop the empty frame below the page. False when the page fills the whole frame
    (it may be cut off)."""
    from PIL import Image
    im = Image.open(png).convert("RGB").crop((0, 0, PHONE, TALL))
    bg = im.getpixel((PHONE // 2, TALL - 2))
    bottom = TALL
    while bottom > 1 and all(im.getpixel((x, bottom - 1)) == bg for x in range(0, PHONE, 5)):
        bottom -= 1
    if bottom > TALL - 50:
        return False
    im.crop((0, 0, PHONE, min(TALL, bottom + 24))).save(png)
    return True


if __name__ == "__main__":
    sys.exit(main())
