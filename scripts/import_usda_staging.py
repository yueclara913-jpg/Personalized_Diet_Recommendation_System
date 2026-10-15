"""Import parser JSON into a separate pending-only staging database."""
import argparse
import json
import sqlite3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from nutrition.importers.usda_sqlite import import_payload, read_payload


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input',help='JSON output from the merged USDA parser, NOT raw Foundation JSON')
    parser.add_argument('--mode',choices=('dry-run','staging'),default='dry-run')
    parser.add_argument('--database',help='Dedicated staging SQLite file; business database refused')
    parser.add_argument('--confirm-existing-staging',action='store_true')
    args=parser.parse_args()
    try:
        report=import_payload(read_payload(args.input),mode=args.mode,database=args.database,
                              confirm_existing=args.confirm_existing_staging)
    except (ValueError,OSError,sqlite3.Error) as error:
        parser.exit(2,json.dumps(dict(status='rejected',error_type=type(error).__name__,
                                      message=str(error)),ensure_ascii=False)+'\n')
    print(json.dumps(report,ensure_ascii=False,sort_keys=True,allow_nan=False,indent=2))


if __name__=='__main__':
    main()
