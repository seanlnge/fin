"""Install the fin MCP skill for Claude; the fin MCP server must be enabled separately."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=Path.home() / ".claude" / "skills" / "fin")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    content = (root / "skill" / "SKILL.md").read_text(encoding="utf-8")
    target = args.target.expanduser() / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Refuse to overwrite a user-maintained skill.
    with target.open("x", encoding="utf-8") as handle:
        handle.write(content)
    print(json.dumps({"installed": str(target), "requires": "The fin MCP server enabled in Claude"}))


if __name__ == "__main__":
    main()
