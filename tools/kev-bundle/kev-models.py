"""The models of the Kev folder, pinned in pins.json (revision and SHA-256 of every file).

  kev-models.py fetch            download the pinned revisions (resumes; a file whose hash differs is fetched again)
  kev-models.py verify           every pinned file has its hash, and nothing else that could hold code is there
  kev-models.py verify --quick   the same, but hashes only the pickle files (head.pt, 5 MB): start-kev.cmd runs it
                                 before every start, because torch reads head.pt with its unpickler

The base model lives in the Hugging Face cache layout (hf/hub/...) at the revision head.pt names, so Kev finds it
offline as it is and head.pt is never rewritten.
"""
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PICKLE = {".pt", ".pth", ".bin", ".pkl", ".pickle", ".ckpt"}
CODE = PICKLE | {".py", ".pyc", ".pyd", ".dll", ".so", ".exe"}
# the base model from an older Kev folder (models/<name>, made by localize.py); moved into the cache when its hashes match
OLD_BASE = HERE / "models" / "Qwen3.5-4B-Base"


def pins():
    return json.loads((HERE / "pins.json").read_text(encoding="utf-8"))["models"]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def problems(quick=False):
    found = []
    for name, m in pins().items():
        folder = HERE / m["dir"]
        for rel, want in m["files"].items():
            p = folder / rel
            if not p.is_file():
                found.append(f"{name}: {rel} がありません")
            elif (not quick or p.suffix in PICKLE) and sha256(p) != want:
                found.append(f"{name}: {rel} のハッシュが違います（改ざんか取得の失敗）")
        if folder.is_dir():
            for p in folder.rglob("*"):
                rel = p.relative_to(folder).as_posix()
                if p.is_file() and p.suffix in CODE and rel not in m["files"] and not rel.startswith(".cache/"):
                    found.append(f"{name}: 一覧に無いファイル {rel}（コードを含みうる種類）")
    return found


def fetch():
    from huggingface_hub import snapshot_download
    for name, m in pins().items():
        folder = HERE / m["dir"]
        folder.mkdir(parents=True, exist_ok=True)
        if name == "base" and OLD_BASE.is_dir():
            for rel, want in m["files"].items():
                src, dst = OLD_BASE / rel, folder / rel
                if src.is_file() and not dst.exists() and sha256(src) == want:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                    print(f"  {rel}: 以前の models の中から移しました（ハッシュ一致）")
        for rel, want in m["files"].items():
            p = folder / rel
            if p.is_file() and sha256(p) != want:
                p.unlink()   # fetched again below: snapshot_download keeps a local file it thinks is current
        snapshot_download(m["repo"], revision=m["revision"], local_dir=str(folder))
    if OLD_BASE.is_dir():
        shutil.rmtree(OLD_BASE, ignore_errors=True)


def main(argv):
    if argv[:1] == ["fetch"]:
        fetch()
        argv = ["verify"]
    if argv[:1] != ["verify"]:
        print(__doc__)
        return 2
    found = problems(quick="--quick" in argv)
    for line in found:
        print("NG " + line)
    if not found:
        print("OK モデルのファイルは固定した版と一致しています" + ("（pickle のみ照合）" if "--quick" in argv else ""))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
