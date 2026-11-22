"""分类体系领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。

停用归档能力：分类状态按 已发布→处理中→已停用 顺序流转；停用后归档旧
分类，根据分类树数据（替代分类编号）与已审核标注生成存量标签转挂数据，
同步到元数据标注的返修清单。归档幂等：对同一原分类重复提交归档只生效
一次；已停用分类不再接受新审核标注（由元数据标注侧拦截）。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.services.base import BaseModuleService
from app.services.metadata_tag import append_review_trail
from app.store import store


class TaxonomyService(BaseModuleService):
    MODULE = "taxonomy"
    ENTITY = "分类树"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["分类编号", "分类名称", "父分类", "替代分类", "层级深度", "关联实体", "分类规则", "自动归类", "最后更新"]
    REQUIRED_FIELDS = ["分类编号", "分类名称", "父分类"]
    STATUS_ORDER = ["草稿", "已发布", "处理中", "已停用"]
    ACTION_RULES = {
        "发布分类": "已发布",
        "进入处理": "处理中",
        "停用分类": "已停用",
        "重构层级": "重构中",
        "合并分类": "已合并",
    }
    NEGATIVE_ACTIONS = []
    ARCHIVE_ACTION = "归档分类"
    TERMINAL_STATUS = "已停用"
    LIVE_STATUS = "已发布"
    # 停用主流程只允许按 已发布→处理中→已停用 顺序流转
    FLOW_SOURCES = {"进入处理": {"已发布"}, "停用分类": {"处理中"}}

    # ===== 跨模块数据流实现 =====
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == self.ARCHIVE_ACTION:
            return self._archive_deprecated(entry_id)
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        code = str(entry.get("分类编号", ""))
        if entry.get("status") == self.TERMINAL_STATUS:
            return None, f"分类{code}已停用，仅可执行「{self.ARCHIVE_ACTION}」"
        sources = self.FLOW_SOURCES.get(action)
        if sources is not None and entry.get("status") not in sources:
            expected = "、".join(sorted(sources))
            return None, (
                f"分类{code}当前为「{entry.get('status')}」，「{action}」只能从「{expected}」流转，"
                "分类状态须按 已发布→处理中→已停用 顺序流转"
            )
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if entry.get("status") == self.TERMINAL_STATUS:
            entry["pending"] = True  # 停用后仍待归档，归档完成才清零
        # 链路 L20: taxonomy → govern_dash（分类发布/重构后触发治理看板更新）
        if entry.get("status") in ("已发布", "重构中"):
            dash_msg = self._push_to_govern_dash(entry)
            message += "；" + dash_msg
        # 链路 L21: taxonomy → metadata_tag（分类变更可触发重新标注）
        if entry.get("status") in ("重构中", "已合并"):
            tag_msg = self._push_to_metadata_tag(entry)
            message += "；" + tag_msg
        return entry, message

    def _archive_deprecated(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """停用归档：根据分类树数据与已审核标注生成存量标签转挂数据。

        - 已审核标注保持原分类编号与原标注批次不变，仅生成指向替代分类的
          转挂记录，写入元数据标注的返修清单（转挂数据记录返修实际采用的
          分类编号）；
        - 仍在返修中的存量件改挂到替代分类，原分类编号留痕，返修人员可在
          替代分类继续领件；
        - 归档幂等：重复提交只生效一次。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        code = str(entry.get("分类编号", ""))
        if entry.get("status") != self.TERMINAL_STATUS:
            return None, f"分类{code}尚未停用，不能归档（须按 已发布→处理中→已停用 流转）"
        archived = entry.get("归档信息")
        if archived:
            return entry, (
                f"分类{code}已于{archived.get('归档时间', '')}归档，"
                f"转挂数据已写入替代分类{archived.get('替代分类', '')}，重复提交不再生效"
            )
        replacement_code = str(entry.get("替代分类") or "").strip()
        replacement = self._find_by_code(replacement_code) if replacement_code else None
        if replacement is None:
            return None, f"分类{code}未配置有效的替代分类，无法生成存量标签转挂数据"
        if replacement.get("status") != self.LIVE_STATUS:
            return None, (
                f"替代分类{replacement_code}当前为「{replacement.get('status')}」，"
                "未处于已发布接件状态，不能承接存量转挂"
            )
        today = date.today().isoformat()
        tag_rows = store.rows("metadata_tag")
        approved = [
            row for row in tag_rows
            if str(row.get("分类编号", "")) == code and row.get("status") == "已审核"
        ]
        reworking = [
            row for row in tag_rows
            if str(row.get("分类编号", "")) == code and row.get("status") == "返修中"
        ]
        next_id = max((int(row.get("id", 0)) for row in tag_rows), default=0)
        for row in approved:
            next_id += 1
            reattach: dict[str, Any] = {
                "id": next_id,
                "status": "返修中",
                "pending": True,
                "abnormal": False,
                "任务编号": f"META-RE-{next_id:04d}",
                "标注对象": str(row.get("标注对象", "")),
                "标注类型": "存量转挂",
                "标注人": "system-archive",
                "标注批次": f"batch-reattach-{next_id:04d}",
                "内容标签": str(row.get("内容标签", "")),
                "分类编号": replacement_code,
                "审核状态": "pending",
                "标注时间": today,
                "原任务编号": str(row.get("任务编号", "")),
                "原分类编号": code,
                "原标注批次": str(row.get("标注批次", "")),
            }
            append_review_trail(
                reattach, "停用归档",
                note=f"分类{code}停用归档，存量标签转挂至{replacement_code}")
            tag_rows.append(reattach)
        for row in reworking:
            row.setdefault("原分类编号", code)
            row["分类编号"] = replacement_code
            append_review_trail(
                row, "停用转挂",
                note=f"分类{code}停用归档，返修件改挂至{replacement_code}")
        entry["归档信息"] = {
            "归档时间": today,
            "替代分类": replacement_code,
            "转挂数量": len(approved),
            "返修重定向": len(reworking),
        }
        entry["pending"] = False
        return entry, (
            f"分类{code}已归档：生成存量标签转挂数据{len(approved)}条、"
            f"重定向返修件{len(reworking)}条，均已写入替代分类{replacement_code}的返修清单"
        )

    def _find_by_code(self, code: str) -> dict[str, Any] | None:
        """按分类编号定位分类：记录归属只认编号，分类重名不影响。"""
        for row in store.rows(self.MODULE):
            if str(row.get("分类编号", "")) == code:
                return row
        return None

    def _push_to_govern_dash(self, entry: dict[str, Any]) -> str:
        rows = store.rows("govern_dash")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        gd_entry: dict[str, Any] = {
            "id": next_id,
            "status": "草稿",
            "pending": True,
            "abnormal": False,
            "指标编号": f"GOVE-TAX-{next_id:04d}",
            "指标名称": f"分类覆盖-{entry.get('分类名称','')}",
            "评估对象": str(entry.get("分类编号", "")),
            "目标值": "0.90",
            "当前值": str(entry.get("层级深度", "0")),
            "趋势方向": "stable",
            "更新频率": "on-classification-change",
            "最后刷新": str(entry.get("最后更新", "2026-11-18")),
        }
        rows.append(gd_entry)
        return f"治理指标同步: {gd_entry['指标编号']}"

    def _push_to_metadata_tag(self, entry: dict[str, Any]) -> str:
        rows = store.rows("metadata_tag")
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        mt_entry: dict[str, Any] = {
            "id": next_id,
            "status": "待分配",
            "pending": True,
            "abnormal": False,
            "任务编号": f"META-TAX-{next_id:04d}",
            "标注对象": str(entry.get("关联实体", "")),
            "标注类型": "re-classify",
            "标注人": "system-auto",
            "标注批次": f"batch-{next_id}",
            "内容标签": str(entry.get("分类名称", "")),
            "分类编号": str(entry.get("分类编号", "")),
            "审核状态": "pending",
            "标注时间": str(entry.get("最后更新", "2026-11-18")),
        }
        rows.append(mt_entry)
        return f"已发起重新标注任务 {mt_entry['任务编号']}"

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["cross_module_consumers"] = ["govern_dash", "metadata_tag"]
        base["archived"] = sum(
            1 for row in store.rows(self.MODULE) if row.get("归档信息"))
        return base
