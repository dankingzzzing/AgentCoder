"""Build the Homework 2 backup ZIP without secrets, run data or development caches."""

import sys
import argparse
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {".vscode", ".git", ".agentcoder", ".qa", ".venv", "__pycache__", "build", "dist", "artifacts", "node_modules", ".pytest_cache"}


def included(path):
    relative = path.relative_to(ROOT)
    return not any(p in EXCLUDED or p.endswith(".egg-info") for p in relative.parts) and not (
        path.name.startswith(".env") and path.name != ".env.example") and path.suffix not in {".pyc", ".pyo", ".key", ".pem"}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="生成不含本机设置和内部运行数据的提交包")
    parser.add_argument('--name',default='homework2-submission',help='备份提交时填写学号姓名，仅用于本机包名')
    name = parser.parse_args().name
    if not name or len(name)>120 or name in {'.','..'} or any(c in name for c in '/\\<>:"|?*\x00'):
        raise SystemExit('提交包名称必须是普通文件名。')
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
