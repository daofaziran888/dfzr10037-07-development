"""停用分类 × 返修并发 验证脚本。

场景：一个有很多历史标注的父分类准备停用，其中一支仍有返修件，替代分类
已开始接件，处理中的标注在旧分类与替代分类之间发生归属冲突。用一个已审
核标注、一个返修中标注和一个等待提交的标注验证：

1. 分类状态只按 已发布→处理中→已停用 顺序流转；
2. 停用流转期间返修人员可继续领件、审计员可重放此前的审核；
3. 停用后归档只生效一次（幂等），且不再接受新审核标注；
4. 已审核内容仍能从原分类编号+原标注批次定位，分类重名不改变记录归属；
5. 停用结果同步到元数据标注的返修清单，转挂数据写入返修实际采用的分类
   编号，归档完成后才能在替代分类领件。

运行：cd backend && .venv/bin/python tests/test_taxonomy_retire.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.metadata_tag import MetadataTagService
from app.services.taxonomy import TaxonomyService
from app.store import store

CHECKS = 0


def check(condition: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"验证失败：{label}")
    print(f"  ✔ {label}")


tax = TaxonomyService()
tag = MetadataTagService()

OLD_CODE = "TAXO-OLD-9001"   # 准备停用的历史父分类
NEW_CODE = "TAXO-NEW-9002"   # 已开始接件的替代分类
DUP_CODE = "TAXO-DUP-9003"   # 与旧分类重名的干扰分类


def make_category(code: str, name: str, replacement: str = "") -> dict:
    entry, missing = tax.create_entry({
        "分类编号": code, "分类名称": name, "父分类": "ROOT",
        "替代分类": replacement, "层级深度": "1", "关联实体": "ENT-DEMO",
        "分类规则": "manual", "自动归类": "false", "最后更新": "2026-11-22",
    })
    assert not missing, f"登记分类缺字段：{missing}"
    return entry


def make_tag(no: str, code: str, batch: str) -> dict:
    entry, missing = tag.create_entry({
        "任务编号": no, "标注对象": "OBJ-DEMO", "标注类型": "manual",
        "标注人": "tester", "标注批次": batch, "内容标签": "历史标签",
        "分类编号": code, "审核状态": "pending", "标注时间": "2026-11-22",
    })
    assert not missing, f"登记标注缺字段：{missing}"
    return entry


def rework_items(code: str) -> list[dict]:
    """元数据标注返修清单中挂在指定分类编号下的返修件。"""
    return [
        row for row in store.rows("metadata_tag")
        if str(row.get("分类编号", "")) == code and row.get("status") == "返修中"
    ]


print("1) 准备分类树数据与三条存量标注（已审核/返修中/等待提交）")
old = make_category(OLD_CODE, "历史父分类", replacement=NEW_CODE)
new = make_category(NEW_CODE, "替代分类")
entry, _ = tax.run_action(old["id"], "发布分类")
check(entry is not None and old["status"] == "已发布", "旧分类已发布")
entry, _ = tax.run_action(new["id"], "发布分类")
check(entry is not None and new["status"] == "已发布", "替代分类已发布、开始接件")

a1 = make_tag("META-OLD-01", OLD_CODE, "B-1001")
tag.run_action(a1["id"], "分配任务")
tag.run_action(a1["id"], "提交审核")
check(a1["status"] == "已审核" and a1["分类编号"] == OLD_CODE, "已审核标注就位（归属旧分类）")

a2 = make_tag("META-OLD-02", OLD_CODE, "B-1002")
tag.run_action(a2["id"], "分配任务")
tag.run_action(a2["id"], "提交审核")
tag.run_action(a2["id"], "发起返修")
check(a2["status"] == "返修中", "返修中标注就位")

a3 = make_tag("META-OLD-03", OLD_CODE, "B-1003")
tag.run_action(a3["id"], "分配任务")
check(a3["status"] == "标注中", "等待提交的标注就位")

print("2) 分类状态只按 已发布→处理中→已停用 顺序流转")
entry, msg = tax.run_action(old["id"], "停用分类")
check(entry is None and old["status"] == "已发布", "已发布不能直接停用")
entry, msg = tax.run_action(old["id"], "进入处理")
check(entry is not None and old["status"] == "处理中", "已发布→处理中")
entry, msg = tax.run_action(old["id"], "进入处理")
check(entry is None and old["status"] == "处理中", "处理中不能重复进入处理")
entry, msg = tax.run_action(old["id"], "归档分类")
check(entry is None, "未停用不能归档")

print("3) 停用流转期间：返修继续领件、审计重放此前审核")
entry, _ = tag.run_action(a2["id"], "分配任务")
check(entry is not None and a2["status"] == "标注中", "流转期间返修人员可继续领件")
entry, _ = tag.run_action(a2["id"], "发起返修")
check(entry is not None and a2["status"] == "返修中", "返修件退回返修清单（停用前仍有返修件）")
trail = tag.replay_review(a1["id"])
check(trail is not None and any(t["动作"] == "提交审核" for t in trail),
      "流转期间审计员可重放已审核标注的审核轨迹")

print("4) 停用：不再接受新审核标注，分类进入终态")
entry, _ = tax.run_action(old["id"], "停用分类")
check(entry is not None and old["status"] == "已停用", "处理中→已停用")
check(old["pending"] is True, "停用后仍待归档")
entry, msg = tax.run_action(old["id"], "发布分类")
check(entry is None, "已停用分类不可再流转（仅可归档）")
entry, msg = tag.run_action(a3["id"], "提交审核")
check(entry is None and "已停用" in msg and a3["status"] == "标注中",
      "等待提交的标注提交审核被停用分类拒绝")

print("5) 归档前：替代分类的返修清单还领不到旧分类的件")
check(len(rework_items(NEW_CODE)) == 0, "归档前替代分类下无可领返修件")

print("6) 归档：生成存量标签转挂数据并同步返修清单，且只生效一次")
entry, msg = tax.run_action(old["id"], "归档分类")
check(entry is not None and old.get("归档信息"), "旧分类归档成功")
check(old["归档信息"]["转挂数量"] == 1 and old["归档信息"]["返修重定向"] == 1,
      "转挂数据=已审核标注1条，返修重定向=返修件1条")
check(old["pending"] is False, "归档完成后不再待处理")
reattach = [row for row in rework_items(NEW_CODE) if row.get("原分类编号") == OLD_CODE]
check(len(reattach) == 2, "停用结果已同步到替代分类的返修清单（转挂1条+改挂返修件1条）")
generated = [row for row in reattach if row.get("标注类型") == "存量转挂"]
check(len(generated) == 1
      and generated[0]["分类编号"] == NEW_CODE
      and generated[0]["原标注批次"] == "B-1001",
      "转挂数据写入返修实际采用的分类编号并保留原标注批次")
check(a1["status"] == "已审核" and a1["分类编号"] == OLD_CODE,
      "已审核标注仍挂在原分类编号下（归属不变）")
check(a2["分类编号"] == NEW_CODE and a2.get("原分类编号") == OLD_CODE,
      "返修件改挂替代分类并留痕原分类编号")
row_count = len(store.rows("metadata_tag"))
entry, msg = tax.run_action(old["id"], "归档分类")
check(entry is not None and "不再生效" in msg, "重复提交归档返回不再生效")
check(len(store.rows("metadata_tag")) == row_count, "重复归档不产生新数据（幂等）")

print("7) 归档后：可在替代分类领件并完成返修")
entry, _ = tag.run_action(generated[0]["id"], "分配任务")
check(entry is not None and generated[0]["status"] == "标注中", "替代分类返修清单可领件")
entry, _ = tag.run_action(generated[0]["id"], "提交审核")
check(entry is not None and generated[0]["status"] == "已审核"
      and generated[0]["分类编号"] == NEW_CODE,
      "转挂件在替代分类审核通过（替代分类已发布接件）")
entry, _ = tag.run_action(a2["id"], "分配任务")
entry, _ = tag.run_action(a2["id"], "提交审核")
check(a2["status"] == "已审核" and a2["分类编号"] == NEW_CODE,
      "原返修件在替代分类完成返修")
entry, msg = tag.run_action(a3["id"], "提交审核")
check(entry is None and a3["分类编号"] == OLD_CODE,
      "仍挂旧分类编号的等待提交标注继续被拒绝")

print("8) 历史标注批次复查：原编号+原批次可定位，重名不改归属")
dup = make_category(DUP_CODE, "历史父分类")
check(dup["分类编号"] != OLD_CODE and dup["分类名称"] == old["分类名称"],
      "存在与旧分类重名的分类")
found = tag.locate_approved(OLD_CODE, "B-1001")
check([row["id"] for row in found] == [a1["id"]],
      "已审核内容仍能从原分类编号+原标注批次唯一定位")
items, total = tag.list_entries(
    filters={"分类编号": OLD_CODE, "标注批次": "B-1001"}, status="已审核")
check(total == 1 and items[0]["id"] == a1["id"], "列表接口按原编号+原批次复查历史标注")
trail = tag.replay_review(a1["id"])
check(trail is not None and [t["动作"] for t in trail] == ["分配任务", "提交审核"],
      "停用归档后审计员仍可重放此前的审核轨迹")
check(store.find("metadata_tag", a1["id"])["分类编号"] == OLD_CODE,
      "分类重名不改变记录归属（仍归属原分类编号）")

print(f"\n全部 {CHECKS} 项验证通过：停用与返修并发行为符合预期。")
