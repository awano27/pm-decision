"""Rebuild the README's picture and sample page from a real `kimeru try` run (nothing is drawn by hand).

    python tools/readme_assets.py            # docs/sample/try-report.html and assets/readme/try-report.png

The run happens in a fresh temporary folder with no KIMERU_* setting, so the page shows `out\\try` and no path of this PC.
The picture is taken by a headless Chrome or Edge (the first that writes one). assets/readme/try-report-mobile.png is a full-page
picture of the same sample page at a phone-wide viewport (375 px) (taken with Playwright: a headless browser window cannot be made
narrower than 500 px).
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BROWSERS = (r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "/usr/bin/google-chrome", "/usr/bin/chromium")
SHOTS = {"try-report.png": (1100, 1660)}


def main():
    with tempfile.TemporaryDirectory() as d:
        env = {k: v for k, v in os.environ.items() if not k.startswith("KIMERU_")}
        env["PYTHONPATH"] = str(ROOT)
        subprocess.run([sys.executable, "-m", "kimeru", "try"], cwd=d, env=env, check=True, stdout=subprocess.DEVNULL)
        sample = ROOT / "docs" / "sample" / "try-report.html"
        sample.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(d) / "out" / "try" / "report.html", sample)
        print(sample)
    for name, (w, h) in SHOTS.items():
        shot = ROOT / "assets" / "readme" / name
        if not any(_shoot(b, sample, shot, w, h) for b in BROWSERS if Path(b).is_file()):
            print("no headless browser could take the picture: the pictures were not taken")
            return 1
        print(shot)
    return 0


def _shoot(browser, page, shot, w, h):
    """One picture; False when this browser wrote none (a managed Edge may refuse headless runs)."""
    with tempfile.TemporaryDirectory() as tmp:   # an ASCII-only temporary path: some browsers fail on others
        png = Path(tmp) / "shot.png"
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1", "--blink-settings=preferredColorScheme=1",
                        f"--user-data-dir={Path(tmp) / 'profile'}", f"--window-size={w},{h}", f"--screenshot={png}", page.as_uri()],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        if not png.is_file():
            return False
        shutil.copyfile(png, shot)
        return True


if __name__ == "__main__":
    sys.exit(main())
