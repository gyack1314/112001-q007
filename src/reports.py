"""已发送日报的不可变快照存储。

日报一旦发送即固化：后续迟到事件、撤销、更正只影响“当前重算”，
不回写已发送版本。同一报告日期可多次发送，版本递增，历史版本
全部保留，各自携带数据截止时点（data_as_of）与输入事件清单
（决策依据）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Union

from .engine import FactRow


@dataclass
class Report:
    report_date: date
    version: int
    sent_at: datetime
    data_as_of: datetime
    rows: List[FactRow] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "report_date": self.report_date.isoformat(),
            "version": self.version,
            "sent_at": self.sent_at.isoformat(),
            "data_as_of": self.data_as_of.isoformat(),
            "rows": [r.to_dict() for r in self.rows],
        }

    @staticmethod
    def from_dict(d: dict) -> "Report":
        return Report(
            report_date=date.fromisoformat(d["report_date"]),
            version=d["version"],
            sent_at=datetime.fromisoformat(d["sent_at"]),
            data_as_of=datetime.fromisoformat(d["data_as_of"]),
            rows=[FactRow.from_dict(r) for r in d.get("rows", [])],
        )


class ReportStore:
    """按 (报告日期, 版本) 留存日报快照；已发送版本永不修改。"""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self._reports: List[Report] = []
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self._reports = [Report.from_dict(d) for d in payload.get("reports", [])]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"reports": [r.to_dict() for r in self._reports]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def send(
        self,
        report_date: date,
        rows: List[FactRow],
        data_as_of: datetime,
        sent_at: Optional[datetime] = None,
    ) -> Report:
        """发送（留存）某报告日期的新版本快照。"""
        version = 1 + max(
            (r.version for r in self._reports if r.report_date == report_date),
            default=0,
        )
        report = Report(
            report_date=report_date,
            version=version,
            sent_at=sent_at or datetime.now(),
            data_as_of=data_as_of,
            rows=list(rows),
        )
        self._reports.append(report)
        self._save()
        return report

    def get(self, report_date: date, version: Optional[int] = None) -> Report:
        """查看已发送日报；缺省返回最新版本。"""
        versions = [r for r in self._reports if r.report_date == report_date]
        if not versions:
            raise KeyError(f"{report_date.isoformat()} 没有已发送的日报")
        if version is None:
            return max(versions, key=lambda r: r.version)
        for r in versions:
            if r.version == version:
                return r
        raise KeyError(f"{report_date.isoformat()} 没有版本 {version} 的日报")

    def list(self) -> List[Report]:
        return sorted(self._reports, key=lambda r: (r.report_date, r.version))
