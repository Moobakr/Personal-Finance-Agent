"""Build a source-only release from an explicit allowlist; never zip the project wholesale."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

PATTERNS = (
    "app.py", "streamlit_app.py", "test.py", ".gitignore", "README.md", "DEPLOYMENT.md", "VALIDATION.md",
    "requirements*.txt", "finance_agent/*.py", "tests/*.py", "scripts/*.py",
    ".streamlit/*.toml.example", ".github/workflows/*.yml", "examples/*.csv",
    "knowledge/**/*.md", "knowledge/**/*.txt", "data/fx_fallback.json",
)
SECRET_PATTERN = re.compile(r"sk-or-v1-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-proj-[A-Za-z0-9_-]{20,}")


def release_files(root):
    root = Path(root).resolve()
    candidates = {path for pattern in PATTERNS for path in root.glob(pattern) if path.is_file()}
    files = []
    for path in sorted(candidates):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Refusing an external or linked release file: {path.name}")
        relative = path.relative_to(root).as_posix()
        if SECRET_PATTERN.search(path.read_text(encoding="utf-8-sig")):
            raise ValueError(f"Potential embedded credential in {relative}; release stopped.")
        files.append(path)
    return files


def build_release(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    files = release_files(root)
    required = {"app.py", "streamlit_app.py", "requirements.txt", "README.md", "DEPLOYMENT.md"}
    if not required.issubset({path.relative_to(root).as_posix() for path in files}):
        raise ValueError("Required release files are missing.")
    manifest = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, "Personal-Finance-Agent/" + path.relative_to(root).as_posix())
        archive.writestr("Personal-Finance-Agent/RELEASE_MANIFEST.json", json.dumps(manifest, indent=2))
    return len(files)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count = build_release(Path(__file__).resolve().parents[1], args.output)
    print(f"Packaged {count} source/sample files into {args.output}")
