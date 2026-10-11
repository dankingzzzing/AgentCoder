"""Build the Homework 2 backup ZIP without secrets, run data or development caches."""

import sys
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {".git", ".agentcoder", ".qa", ".venv", "__pycache__", "build", "dist", "artifacts", "node_modules", ".pytest_cache"}


def included(path):
    relative = path.relative_to(ROOT)
    return not any(p in EXCLUDED or p.endswith(".egg-info") for p in relative.parts) and not (
        path.name.startswith(".env") and path.name != ".env.example") and path.suffix not in {".pyc", ".pyo", ".key", ".pem"}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    name = "2412190104唐佳杰"
    output = ROOT / "artifacts" / (name + ".zip")
    output.parent.mkdir(exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(ROOT.rglob("*")):
            if path.is_file() and not path.is_symlink() and included(path):
                archive.write(path, name + "/" + path.relative_to(ROOT).as_posix())
    with ZipFile(output) as archive:
        if archive.testzip():
            raise SystemExit("ZIP 完整性检查失败。")
        count = len(archive.namelist())
    if output.stat().st_size >= 200 * 1024 * 1024:
        raise SystemExit("备份提交包超过 200 MB。")
    print(f"已生成 {output}（{count} 个文件，{output.stat().st_size/1024:.1f} KiB）")


if __name__ == "__main__":
    main()
