from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED_KEYWORDS = [
    "市场假设",
    "信号",
    "风险",
    "执行",
    "收益来源",
    "失效条件",
    "交易成本",
    "滑点",
    "仓位管理",
    "最大回撤",
    "样本外",
    "过拟合",
    "角色 A",
    "角色 B",
    "最终结论",
    "hold_logic",
    "reduce_logic",
    "add_logic",
    "exit_logic",
    "time_stop",
    "review_metrics",
    "MFE",
    "MAE",
    "R_multiple",
]


def check_report(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    missing = [keyword for keyword in REQUIRED_KEYWORDS if keyword not in text]
    return {
        "ok": not missing,
        "report": str(path),
        "missing": missing,
        "required_count": len(REQUIRED_KEYWORDS),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check that a strategy report contains the required research sections.")
    parser.add_argument("--report", required=True, help="Path to a strategy report markdown file.")
    args = parser.parse_args()

    path = Path(args.report)
    if not path.exists():
        print(json.dumps({"ok": False, "error": "report_not_found", "report": str(path)}, ensure_ascii=False, indent=2))
        return 2

    result = check_report(path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
