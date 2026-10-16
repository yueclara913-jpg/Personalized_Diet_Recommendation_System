"""Initialize a new local health-profile database; no implicit target path."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nutrition.profile.repository import initialize_database


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    args = parser.parse_args(argv)
    initialize_database(args.database)
    print("健康档案数据库已初始化；当前没有公共用户查询接口。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
