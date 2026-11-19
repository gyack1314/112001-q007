"""命令行入口：日事实重算、日报发送与查看、冲突清单、演示。

用法（仓库根目录下）：
    python -m src.cli facts     --date 2026-04-05 [--as-of 2026-04-08]
    python -m src.cli send      --date 2026-04-05 [--as-of 2026-04-05]
    python -m src.cli report    --date 2026-04-05 [--version 1]
    python -m src.cli conflicts [--as-of 2026-04-30]
    python -m src.cli list
    python -m src.cli demo
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import unicodedata
from datetime import date, datetime, time
from pathlib import Path
from typing import List, Optional

from .conflicts import INFO, Conflict
from .engine import STATUS_BLOCKED, STATUS_OK, STATUS_WARNING, CapacityEngine, FactRow
from .loader import load_events
from .reports import Report, ReportStore

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = REPO_ROOT / "data" / "events.json"
DEFAULT_STATE = Path("state") / "reports.json"

STATUS_LABEL = {
    STATUS_OK: "正常",
    STATUS_WARNING: "警告",
    STATUS_BLOCKED: "阻塞",
}
SEVERITY_LABEL = {"BLOCKING": "阻塞", "WARNING": "警告", "INFO": "提示"}


# ----------------------------------------------------------------------
# 输出格式化（处理中英文混排宽度）
# ----------------------------------------------------------------------
def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _render_table(header: List[str], rows: List[List[str]]) -> str:
    widths = [
        max([_width(h)] + [_width(r[i]) for r in rows]) if rows else _width(h)
        for i, h in enumerate(header)
    ]
    lines = ["  ".join(_pad(h, widths[i]) for i, h in enumerate(header))]
    lines.append("  ".join("-" * widths[i] for i in range(len(header))))
    for r in rows:
        lines.append("  ".join(_pad(cell, widths[i]) for i, cell in enumerate(r)))
    return "\n".join(lines)


def _fmt(value) -> str:
    return "—" if value is None else str(value)


def render_facts(rows: List[FactRow]) -> str:
    header = ["日期", "园区", "资源", "安装容量", "预留", "停机(日终)", "停机(峰值)", "可用量", "状态"]
    body = [
        [
            r.date.isoformat(),
            r.park,
            r.resource,
            _fmt(r.installed_capacity),
            str(r.reserved_qty),
            str(r.downtime_qty),
            str(r.downtime_peak_qty),
            _fmt(r.available_qty),
            STATUS_LABEL.get(r.status, r.status),
        ]
        for r in rows
    ]
    return _render_table(header, body)


def render_conflict_details(conflicts: List[Conflict]) -> str:
    lines = []
    for c in conflicts:
        label = SEVERITY_LABEL.get(c.severity, c.severity)
        when = c.date.isoformat() if c.date else "-"
        lines.append(f"  [{label}] {c.kind} @ {when}（{c.park}/{c.resource}）：{c.message}")
    return "\n".join(lines)


def render_conflicts(conflicts: List[Conflict]) -> str:
    header = ["级别", "种类", "园区", "资源", "首发日期", "范围", "说明"]
    scope_label = {"date": "当日", "onward": "当日及以后"}
    body = [
        [
            SEVERITY_LABEL.get(c.severity, c.severity),
            c.kind,
            c.park,
            c.resource,
            c.date.isoformat() if c.date else "-",
            scope_label.get(c.scope, c.scope),
            c.message,
        ]
        for c in conflicts
    ]
    return _render_table(header, body)


def print_facts_with_conflicts(rows: List[FactRow]) -> None:
    print(render_facts(rows))
    for r in rows:
        visible = [c for c in r.conflicts]
        if visible:
            print(f"\n{r.date.isoformat()} {r.park}/{r.resource} 冲突明细：")
            print(render_conflict_details(visible))


# ----------------------------------------------------------------------
# 参数解析
# ----------------------------------------------------------------------
def _parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD：{text!r}") from None


def _parse_as_of(text: Optional[str]) -> Optional[datetime]:
    """as-of 知晓时点；裸日期表示当日结束（23:59:59）。"""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--as-of 格式应为 YYYY-MM-DD 或 ISO 时间：{text!r}"
        ) from None
    if "T" not in text and " " not in text:
        dt = datetime.combine(dt.date(), time.max)
    return dt


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", default=str(DEFAULT_DATA), help="事件文件路径")
    common.add_argument(
        "--state", default=str(DEFAULT_STATE), help="日报快照存储路径"
    )

    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="园区算力容量日事实与日报版本管理",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("facts", parents=[common], help="按业务日期重算日事实（当前视角）")
    p.add_argument("--date", required=True, type=_parse_day, help="业务日期")
    p.add_argument("--as-of", dest="as_of", type=_parse_as_of, default=None,
                   help="知晓时点，缺省为全部已知记录")

    p = sub.add_parser("send", parents=[common], help="发送日报（留存不可变快照）")
    p.add_argument("--date", required=True, type=_parse_day, help="报告日期")
    p.add_argument("--as-of", dest="as_of", type=_parse_as_of, default=None,
                   help="数据截止时点，缺省为全部已知记录")

    p = sub.add_parser("report", parents=[common], help="查看已发送日报")
    p.add_argument("--date", required=True, type=_parse_day, help="报告日期")
    p.add_argument("--version", type=int, default=None, help="版本号，缺省为最新")

    p = sub.add_parser("conflicts", parents=[common], help="查看冲突清单")
    p.add_argument("--as-of", dest="as_of", type=_parse_as_of, default=None)

    sub.add_parser("list", parents=[common], help="列出已发送日报版本")
    sub.add_parser("demo", parents=[common], help="用合成数据演示完整场景")
    return parser


# ----------------------------------------------------------------------
# 子命令
# ----------------------------------------------------------------------
def _load_engine(args) -> CapacityEngine:
    return CapacityEngine(load_events(args.data))


def _as_of_label(as_of: Optional[datetime]) -> str:
    return as_of.isoformat() if as_of else "全部已知记录"


def cmd_facts(args) -> int:
    engine = _load_engine(args)
    rows = engine.facts(args.date, args.as_of)
    print(f"日事实重算：业务日期 {args.date.isoformat()}，数据截止 {_as_of_label(args.as_of)}")
    if not rows:
        print("（该日期没有可计算的事实）")
        return 0
    print_facts_with_conflicts(rows)
    return 0


def cmd_send(args) -> int:
    engine = _load_engine(args)
    rows = engine.facts(args.date, args.as_of)
    store = ReportStore(args.state)
    data_as_of = args.as_of or datetime.now()
    report = store.send(args.date, rows, data_as_of)
    print(
        f"日报已发送：{report.report_date.isoformat()} 版本 v{report.version}，"
        f"数据截止 {report.data_as_of.isoformat()}，共 {len(report.rows)} 行"
    )
    print("（快照已固化，后续重算不影响本版本）")
    print_facts_with_conflicts(report.rows)
    return 0


def cmd_report(args) -> int:
    store = ReportStore(args.state)
    try:
        report = store.get(args.date, args.version)
    except KeyError as exc:
        print(exc.args[0], file=sys.stderr)
        return 1
    print(
        f"已发送日报：{report.report_date.isoformat()} 版本 v{report.version}，"
        f"发送于 {report.sent_at.isoformat()}，数据截止 {report.data_as_of.isoformat()}"
    )
    print("（已发送快照，保留原始决策依据，不受后续重算影响）")
    print_facts_with_conflicts(report.rows)
    return 0


def cmd_conflicts(args) -> int:
    engine = _load_engine(args)
    conflicts = engine.conflicts(args.as_of)
    print(f"冲突清单（数据截止 {_as_of_label(args.as_of)}）：")
    if not conflicts:
        print("（无冲突）")
        return 0
    print(render_conflicts(conflicts))
    blocking = sum(1 for c in conflicts if c.severity == "BLOCKING")
    if blocking:
        print(f"\n其中 {blocking} 项为阻塞级：对应日期的可用量无法可靠判断。")
    return 0


def cmd_list(args) -> int:
    store = ReportStore(args.state)
    reports = store.list()
    if not reports:
        print("（尚无已发送日报）")
        return 0
    print(
        _render_table(
            ["报告日期", "版本", "发送时间", "数据截止"],
            [
                [
                    r.report_date.isoformat(),
                    f"v{r.version}",
                    r.sent_at.isoformat(),
                    r.data_as_of.isoformat(),
                ]
                for r in reports
            ],
        )
    )
    return 0


# ----------------------------------------------------------------------
# 演示：用合成数据走完“迟到/撤销/更正/冲突/日报版本”全场景
# ----------------------------------------------------------------------
def _section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def cmd_demo(args) -> int:
    engine = _load_engine(args)
    with tempfile.TemporaryDirectory() as tmp:
        store = ReportStore(Path(tmp) / "reports.json")

        _section("[1] 2026-04-05 当日视角（as-of 2026-04-05）：恢复事件尚未到达")
        day = date(2026, 4, 5)
        as_of_early = _parse_as_of("2026-04-05")
        print_facts_with_conflicts(engine.facts(day, as_of_early))

        _section("[2] 发送 2026-04-05 日报（快照 v1 固化，作为当日决策依据）")
        report = store.send(day, engine.facts(day, as_of_early), data_as_of=as_of_early)
        print(f"已发送：{report.report_date.isoformat()} v{report.version}，"
              f"数据截止 {report.data_as_of.isoformat()}")

        _section("[3] 迟到的恢复事件 E-REC-1 于 04-08 到达（业务时间 04-05 11:00）")
        print("按 as-of 2026-04-08 重算 2026-04-05：停机 15 → 0，可用量相应上调；")
        print("重算从事件日志整体进行，不会出现负容量或重复回补。")
        print_facts_with_conflicts(engine.facts(day, _parse_as_of("2026-04-08")))

        _section("[4] 查看已发送日报：仍为 v1 原始版本，重算不回写")
        sent = store.get(day)
        print(f"已发送日报 {sent.report_date.isoformat()} v{sent.version}，"
              f"数据截止 {sent.data_as_of.isoformat()}：")
        print_facts_with_conflicts(sent.rows)
        row = next(r for r in sent.rows if r.park == "P01")
        print(f"决策依据（输入事件）：{row.inputs}")

        _section("[5] 重复恢复事件 E-REC-5：超出在停数量的回补被截断并告警")
        print("P02/gpu 04-06 收到两次各 10 的恢复，而在停只有 10：")
        print_facts_with_conflicts(engine.facts(date(2026, 4, 6)))

        _section("[6] 撤销与更正：误报停机被撤销、停机数量被更正")
        print("P01 04-10 的停机 E-OUT-2 系误报，04-12 撤销（as-of 04-11 vs 04-12）：")
        print("— as-of 2026-04-11：")
        print(render_facts(engine.facts(date(2026, 4, 10), _parse_as_of("2026-04-11"))))
        print("— as-of 2026-04-12（撤销已知晓，相应期间重算）：")
        print(render_facts(engine.facts(date(2026, 4, 10), _parse_as_of("2026-04-12"))))
        print("P01 04-15 停机数量由 3 更正为 6（E-COR-3，as-of 04-16 起生效）：")
        print(render_facts(engine.facts(date(2026, 4, 15), _parse_as_of("2026-04-16"))))

        _section("[7] 容量台账冲突：P03 两个来源取值 60/80，阻塞而非取乐观值")
        print_facts_with_conflicts(engine.facts(date(2026, 4, 2)))

        _section("[8] 超承诺：P02/gpu 预留合计 55 超过安装容量 50，可用量为负并告警")
        print_facts_with_conflicts(engine.facts(date(2026, 4, 4)))

        _section("[9] 全量冲突清单（as-of 全部已知记录）")
        print(render_conflicts(engine.conflicts()))

    print()
    print("演示结束。可用 python -m src.cli facts/send/report/conflicts 进一步查看。")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    handler = {
        "facts": cmd_facts,
        "send": cmd_send,
        "report": cmd_report,
        "conflicts": cmd_conflicts,
        "list": cmd_list,
        "demo": cmd_demo,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
