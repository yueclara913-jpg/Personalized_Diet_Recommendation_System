"""Initialize a new diet log database at an explicit path."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from nutrition.diet.repository import initialize_database


def main(argv=None):
    parser = argparse.ArgumentParser(); parser.add_argument("--database", required=True)
    args = parser.parse_args(argv); initialize_database(args.database)
    print("饮食记录数据库已初始化；未开放个人记录 HTTP API。")
    return 0


if __name__ == "__main__": raise SystemExit(main())
