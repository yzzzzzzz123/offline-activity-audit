"""Case-scoped audit tools. Does not run a model, callback or live query."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from audit_core.audit_tools import execute_tool

def main():
    if hasattr(sys.stdout, 'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',required=True,type=Path,help='本单已保存的case JSON，包含documents/materials/units')
    parser.add_argument('tool',choices=['reference_fields','linked_application_amounts','pos_calculation','source_inventory','activity_periods','pos_field_inventory','pos_matrix_identity'])
    parser.add_argument('--fields',nargs='*',default=[])
    args=parser.parse_args()
    case=json.loads(args.case.read_text(encoding='utf-8-sig'))
    result=execute_tool(case,args.tool,fields=args.fields)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
