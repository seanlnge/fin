"""Build dist/fin-<version>.mcpb, a one-click Claude Desktop extension.

The bundle uses the MCPB "uv" runtime: Claude Desktop installs Python and the
locked dependencies itself, so users need no Python, git, or terminal.
Only source files are bundled; .env files, databases, and virtualenvs never are.
"""
import argparse
import json
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["pyproject.toml", "uv.lock", "README.md", "LICENSE", "NOTICE"]


def stage(staging: Path) -> str:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "fin").mkdir(parents=True)
    manifest = json.loads((ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = version
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for name in FILES:
        if not (ROOT / name).exists():
            raise SystemExit(f"Missing {name}; run `uv lock` first if uv.lock is missing")
        shutil.copy2(ROOT / name, staging / name)
    for source in sorted((ROOT / "fin").iterdir()):
        if source.suffix in {".py", ".sql"}:
            shutil.copy2(source, staging / "fin" / source.name)
    return version


def pack(staging: Path, out: Path):
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(staging).as_posix())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--validate", action="store_true",
                        help="Also validate the manifest with `npx @anthropic-ai/mcpb validate` (needs Node)")
    args = parser.parse_args()
    staging = args.out_dir / "mcpb-staging"
    version = stage(staging)
    if args.validate:
        subprocess.run(["npx", "--yes", "@anthropic-ai/mcpb", "validate", str(staging / "manifest.json")], check=True)
    out = args.out_dir / f"fin-{version}.mcpb"
    pack(staging, out)
    print(json.dumps({"bundle": str(out), "version": version}))


if __name__ == "__main__":
    main()
