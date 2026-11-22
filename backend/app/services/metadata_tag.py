"""元数据标注领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。

与分类体系联动：标注通过分类编号归属分类（分类重名不改变记录归属）；
分类停用后不再接受新审核标注，但返修清单在停用流转期间仍可继续领件；
每次动作都写入审核轨迹，供审计员重放此前的审核。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


def append_review_trail(row: dict[str, Any], action: str, *, note: str = "") -> None:
    """把一次流转写进审核轨迹，供审计重放。轨迹随标注记录走，不依赖分类名称。"""
    trail = row.setdefault("审核轨迹", [])
    trail.append({
        "动作": action,
        "时间": date.today().isoformat(),
        "分类编号": str(row.get("分类编号", "")),
        "状态": str(row.get("status", "")),
        "备注": note,
    })


class MetadataTagService(BaseModuleService):
    MODULE = "metadata_tag"
    ENTITY = "标注任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务编号", "标注对象", "标注类型", "标注人", "标注批次", "内容标签", "分类编号", "审核状态", "标注时间"]
    REQUIRED_FIELDS = ["任务编号", "标注对象", "标注类型"]
    STATUS_ORDER = ["待分配", "标注中", "已审核", "返修中"]
    ACTION_RULES = {"分配任务": "标注中", "提交审核": "已审核", "发起返修": "返修中"}
    NEGATIVE_ACTIONS = []

    # ===== 跨模块数据流实现 =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        # 停用分类不再接受新审核标注；分配领件、发起返修等动作不受影响
        if action == "提交审核":
            block = self._deprecated_category_block(entry)
            if block is not None:
                return None, block
        previous = str(entry.get("status", ""))
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        append_review_trail(entry, action, note=f"{previous}→{entry.get('status', '')}")
        # 链路 L18: metadata_tag → taxonomy（标注审核通过后更新分类体系）
        if entry.get("status") == "已审核":
            tax_msg = self._push_to_taxonomy(entry)
            message += "；" + tax_msg
        # 链路 L19: metadata_tag → govern_dash（标注完成更新治理指标）
        if entry.get("status") == "已审核":
            dash_msg = self._push_to_govern_dash(entry)
            message += "；" + dash_msg
        return entry, message

    def _deprecated_category_block(self, entry: dict[str, Any]) -> str | None:
        """提交审核前检查归属分类：已停用分类不接受新审核标注。"""
        code = str(entry.get("分类编号", "")).strip()
        if not code:
            return None
        for row in store.rows("taxonomy"):
            if str(row.get("分类编号", "")) == code:
                if row.get("status") == "已停用":
                    return f"分类{code}已停用，不再接受新审核标注"
                return None
        return None

    def locate_approved(self, category_code: str, batch: str) -> list[dict[str, Any]]:
        """按原分类编号+原标注批次定位已审核内容，分类重名不影响归属。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("status") == "已审核"
            and str(row.get("分类编号", "")) == category_code
            and str(row.get("标注批次", "")) == batch
        ]

    def replay_review(self, entry_id: int) -> list[dict[str, Any]] | None:
        """审计重放：按记录 id 返回该标注的完整审核轨迹。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        return list(entry.get("审核轨迹", []))

    def _push_to_taxonomy(self, entry: dict[str, Any]) -> str:
        rows = store.rows("taxonomy")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        tax_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "分类编号": f"TAXO-TAG-{next_id:04d}",
            "分类名称": str(entry.get("内容标签", "default-tag")),
            "父分类": str(entry.get("标注类型", "root")),
            "替代分类": "",
            "层级深度": "2",
            "关联实体": str(entry.get("标注对象", "")),
            "分类规则": f"from_tag_{entry.get('标注批次','')}",
            "自动归类": "true",
            "最后更新": str(entry.get("标注时间", "2026-11-18")),
        }
        rows.append(tax_entry)
        return f"已更新分类体系 {tax_entry['分类编号']}"

    def _push_to_govern_dash(self, entry: dict[str, Any]) -> str:
        rows = store.rows("govern_dash")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        gd_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "指标编号": f"GOVE-TAG-{next_id:04d}",
            "指标名称": f"标注质量-{entry.get('标注类型','')}",
            "评估对象": str(entry.get("标注对象", "")),
            "目标值": "0.95",
            "当前值": "0.92",
            "趋势方向": "stable",
            "更新频率": "on-tag-approved",
            "最后刷新": str(entry.get("标注时间", "2026-11-18")),
        }
        rows.append(gd_entry)
        return f"已推送治理看板指标 {gd_entry['指标编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["taxonomy", "govern_dash"]
        return base
