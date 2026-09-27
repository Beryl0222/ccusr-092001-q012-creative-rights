"""文创设计权利转换服务的规则测试。

覆盖：监护门槛、展示/商用许可隔离、来源图闭包、重复回调幂等、
驳回重提、授权版本更新、撤回分级影响、水印文件限次限期、
结算按比例分配与幂等、账目/来源图一致性、HTTP 端到端。
"""

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from service import (
    CreativeRightsService, Handler, SERVICE_ID, health, load_domain, ServiceError,
)


def make_service():
    return CreativeRightsService(load_domain())


def seed_full_flow(grant_commercial=True, minor_guardian=True, approve_sample=True):
    """构造一条走到“授权待发”的完整链路，返回各环节 ID。"""
    svc = make_service()
    brief = svc.create_brief("校园文创季", "校徽再设计", "brief-key")
    adult = svc.register_contributor("林老师", minor=False)
    student = svc.register_contributor("小周", minor=True)
    if minor_guardian:
        svc.guardian_confirm(student["id"], "周父", "guard-key")
    c1 = svc.submit_contribution(brief["id"], adult["id"], "文字创意",
                                 "主题文案", idem_key="c1")["贡献"]
    c2 = svc.submit_contribution(brief["id"], student["id"], "原始图像",
                                 "手绘校猫", idem_key="c2")["贡献"]
    svc.grant_purpose(c1, "校内展示", "有效", "g1")
    svc.grant_purpose(c2, "校内展示", "有效", "g2")
    if grant_commercial:
        svc.grant_purpose(c1, "商品生产", "有效", "g3")
        svc.grant_purpose(c2, "商品生产", "有效", "g4")
    # 未监护的未成年素材在进入流水线前即被拦截：链路只用成人素材
    chain_inputs = [c1, c2] if minor_guardian else [c1]
    batch = svc.register_generation_batch(brief["id"], "校猫+祥云", "model-a",
                                          chain_inputs)
    cb = svc.generation_callback(batch["id"], True, "gen://draft1", "cb-key")
    v1 = cb["产出版本"]
    merge = svc.merge_version(brief["id"], "人工修改版", [v1],
                              [c2] if minor_guardian else [],
                              note="调整配色", idem_key="m1")
    v2 = merge["版本"]
    supplier = svc.register_supplier("钱塘印务")
    sid = None
    if approve_sample:
        sid = svc.submit_sample(v2, supplier["id"], "sample://a", "s1")["样品"]
        svc.review_sample(sid, True, idem_key="r1")
    shares = ({adult["id"]: 0.4, student["id"]: 0.6} if minor_guardian
              else {adult["id"]: 1.0})
    own = svc.confirm_ownership(v2, shares, "own1")
    return type("Flow", (), {})() if False else {
        "svc": svc, "brief": brief, "adult": adult, "student": student,
        "c1": c1, "c2": c2, "batch": batch, "v1": v1, "v2": v2,
        "supplier": supplier, "shares": shares, "own": own,
    }


class IdentityTest(unittest.TestCase):
    def test_health(self):
        self.assertEqual(health(), {"status": "ok", "service": SERVICE_ID})

    def test_domain_enums(self):
        d = load_domain()
        for key in ("贡献类型", "用途范围", "授权状态", "版本类型", "样品状态", "商品生产状态"):
            self.assertIn(key, d)


class GuardianTest(unittest.TestCase):
    def test_minor_contribution_blocked_before_guardian(self):
        f = seed_full_flow(minor_guardian=False)
        svc = f["svc"]
        c2 = svc.contributions[f["c2"]]
        # 即使已授展示用途，未监护确认仍不可用于设计
        self.assertFalse(c2["监护就绪"])
        self.assertFalse(c2["可用于设计"])
        with self.assertRaises(ServiceError) as ctx:
            svc.register_generation_batch(
                f["brief"]["id"], "x", "m", [f["c2"]])
        self.assertEqual(ctx.exception.code, "素材不可用")
        # 人工合并时追加该素材同样被拦截
        with self.assertRaises(ServiceError) as ctx:
            svc.merge_version(f["brief"]["id"], "人工修改版", [f["v1"]], [f["c2"]])
        self.assertEqual(ctx.exception.code, "素材不可用")

    def test_ownership_gate_rejects_unconfirmed_guardian_in_closure(self):
        """纵深防御：素材已在来源闭包中但监护确认缺失时，权属确认拒绝。"""
        f = seed_full_flow()
        svc = f["svc"]
        # 撤销监护确认（模拟数据不一致），权属关口仍应挡住
        svc.contributors[f["student"]["id"]]["监护确认"] = None
        with self.assertRaises(ServiceError) as ctx:
            svc.confirm_ownership(f["v2"], f["shares"], "own-guard")
        self.assertEqual(ctx.exception.code, "监护缺失")

    def test_guardian_confirm_is_idempotent_and_unlocks(self):
        f = seed_full_flow(minor_guardian=False)
        svc = f["svc"]
        first = svc.guardian_confirm(f["student"]["id"], "周父", "gk")
        repeat = svc.guardian_confirm(f["student"]["id"], "周父", "gk")
        self.assertTrue(repeat["重复"])
        self.assertTrue(svc.contributions[f["c2"]]["可用于设计"])

    def test_adult_guardian_confirm_rejected(self):
        svc = make_service()
        a = svc.register_contributor("成人", minor=False)
        with self.assertRaises(ServiceError):
            svc.guardian_confirm(a["id"], "谁")


class ScopeSeparationTest(unittest.TestCase):
    def test_display_grant_does_not_extend_to_commercial(self):
        f = seed_full_flow(grant_commercial=False)
        svc, v2 = f["svc"], f["v2"]
        c1 = svc.contributions[f["c1"]]
        self.assertEqual(c1["用途授权"]["校内展示"], "有效")
        self.assertFalse(c1["可商用"])
        # 权属确认可以做（展示合作），但商品生产授权发不出来
        with self.assertRaises(ServiceError) as ctx:
            svc.issue_license(v2, ["商品生产"], "lic-com")
        self.assertEqual(ctx.exception.code, "授权范围越界")
        # 仅展示的授权版本合法（公益展览未授权，不能捎带）
        lic = svc.issue_license(v2, ["校内展示"], "lic-show")
        self.assertEqual(lic["版次"], 1)
        with self.assertRaises(ServiceError):
            svc.register_product("校猫徽章", lic["授权"], f["supplier"]["id"], "p1")

    def test_commercial_license_requires_approved_sample(self):
        f = seed_full_flow(grant_commercial=True, approve_sample=False)
        svc, v2 = f["svc"], f["v2"]
        with self.assertRaises(ServiceError) as ctx:
            svc.issue_license(v2, ["商品生产"], "lic1")
        self.assertEqual(ctx.exception.code, "样品未通过")


class ProvenanceTest(unittest.TestCase):
    def test_merge_requires_parents_and_keeps_closure(self):
        f = seed_full_flow()
        svc = f["svc"]
        with self.assertRaises(ServiceError):
            svc.merge_version(f["brief"]["id"], "人工修改版", [], [])
        prov = svc.provenance(f["v2"])
        self.assertEqual(prov["含自身"][0], f["v2"])
        self.assertIn(f["v1"], prov["祖先版本"])
        self.assertEqual(set(prov["素材闭包"]), {f["c1"], f["c2"]})


class CallbackIdempotencyTest(unittest.TestCase):
    def test_duplicate_callback_creates_one_version(self):
        f = seed_full_flow()
        svc, batch = f["svc"], f["batch"]
        again = svc.generation_callback(batch["id"], True, "gen://draft1", "cb-key")
        self.assertTrue(again["重复"])
        self.assertEqual(batch["回调次数"], 1)
        self.assertEqual(batch["产出版本"], f["v1"])
        third = svc.generation_callback(batch["id"], True, "gen://other", "cb-key")
        self.assertTrue(third["重复"])

    def test_repeated_submission_idempotent(self):
        f = seed_full_flow()
        svc = f["svc"]
        a = svc.submit_contribution(f["brief"]["id"], f["adult"]["id"],
                                    "文字创意", "同一份", idem_key="dup")
        b = svc.submit_contribution(f["brief"]["id"], f["adult"]["id"],
                                    "文字创意", "同一份", idem_key="dup")
        self.assertEqual(a["贡献"], b["贡献"])
        self.assertTrue(b["重复"])


class SampleRoundTest(unittest.TestCase):
    def test_reject_then_resubmit_keeps_history(self):
        f = seed_full_flow(approve_sample=False)
        svc, v2, sup = f["svc"], f["v2"], f["supplier"]
        s1 = svc.submit_sample(v2, sup["id"], "sample://1", "ss1")["样品"]
        r1 = svc.review_sample(s1, False, "色差", "rr1")
        self.assertEqual(r1["状态"], "已驳回")
        # 已结轮次不能重复验收
        with self.assertRaises(ServiceError):
            svc.review_sample(s1, True, idem_key="rr2")
        # 重复回调不改变结论
        replay = svc.review_sample(s1, False, "色差", "rr1")
        self.assertTrue(replay["重复"])
        # 重提是新一轮，历史保留
        r2 = svc.submit_sample(v2, sup["id"], "sample://2", "ss2")
        self.assertFalse(r2["重复"])
        s2 = r2["样品"]
        self.assertEqual(r2["轮次"], 2)
        svc.review_sample(s2, True, idem_key="rr3")
        latest = svc.latest_sample(v2, sup["id"])
        self.assertEqual(latest["id"], s2)
        self.assertEqual(svc.samples[s1]["驳回原因"], "色差")


class LicenseRevisionTest(unittest.TestCase):
    def test_license_update_keeps_old_revision_and_pins_product(self):
        f = seed_full_flow()
        svc, v2, sup = f["svc"], f["v2"], f["supplier"]
        l1 = svc.issue_license(v2, ["商品生产"], "lic1")
        self.assertEqual(l1["版次"], 1)
        p1 = svc.register_product("校猫徽章", l1["授权"], sup["id"], "prod1")
        # 扩展渠道宣传授权（素材需先取得该用途）
        svc.grant_purpose(f["c1"], "渠道宣传", "有效", "g5")
        svc.grant_purpose(f["c2"], "渠道宣传", "有效", "g6")
        # 更新授权：旧版保留，商品仍钉在第 1 版
        l2 = svc.issue_license(v2, ["商品生产", "渠道宣传"], "lic2")
        self.assertEqual(l2["版次"], 2)
        self.assertEqual(l2["上一版"], 1)
        self.assertEqual(svc.products[p1["商品"]]["授权版次"], 1)
        old = svc.license_revision(l1["授权"], 1)
        self.assertEqual(old["状态"], "被替代")
        # 新商品显式挂旧版被拒；不指定版次则取系列最新版
        with self.assertRaises(ServiceError) as ctx:
            svc.register_product("旧授权商品", l1["授权"], sup["id"],
                                 revision=1, idem_key="prod-old")
        self.assertEqual(ctx.exception.code, "授权已更新")
        p2 = svc.register_product("校猫帆布袋", l2["授权"], sup["id"],
                                  idem_key="prod2")
        self.assertEqual(svc.products[p2["商品"]]["授权版次"], 2)


class WithdrawImpactTest(unittest.TestCase):
    def _flow_with_products(self):
        f = seed_full_flow()
        svc, v2, sup = f["svc"], f["v2"], f["supplier"]
        lic = svc.issue_license(v2, ["商品生产"], "lic")["授权"]
        p1 = svc.register_product("待产徽章", lic, sup["id"], "p1")["商品"]
        p2 = svc.register_product("在制钥匙扣", lic, sup["id"], "p2")["商品"]
        p3 = svc.register_product("已售帆布袋", lic, sup["id"], "p3")["商品"]
        svc.advance_production(p2, "在制中", "adv2")
        svc.advance_production(p3, "在制中", "adv3a")
        svc.advance_production(p3, "已售出", "adv3b")
        return f, p1, p2, p3

    def test_withdraw_buckets_impact_and_keeps_records(self):
        f, p1, p2, p3 = self._flow_with_products()
        svc = f["svc"]
        svc.withdraw_contribution(f["c2"], "作者撤回", "wd")
        impact = svc.impact_analysis(f["c2"])
        names_not = {x["商品"] for x in impact["尚未生产"]}
        names_making = {x["商品"] for x in impact["在制"]}
        names_sold = {x["商品"] for x in impact["已售出或已完成"]}
        self.assertEqual(names_not, {p1})
        self.assertEqual(names_making, {p2})
        self.assertEqual(names_sold, {p3})
        self.assertIn(f["v2"], impact["受影响版本"])
        # 记录未删除：贡献、版本、授权、商品、事件都还在
        self.assertEqual(svc.contributions[f["c2"]]["授权状态"], "已撤回")
        self.assertIn(f["v2"], svc.versions)
        self.assertTrue(any(e["类型"] == "撤回" for e in svc.events))

    def test_dispute_blocks_new_use_but_preserves_sales(self):
        f, p1, p2, p3 = self._flow_with_products()
        svc = f["svc"]
        svc.dispute_contribution(f["c1"], "CASE-7", "dp")
        self.assertEqual(svc.contributions[f["c1"]]["授权状态"], "争议中")
        self.assertFalse(svc.contributions[f["c1"]]["可用于设计"])
        impact = svc.impact_analysis(f["c1"])
        self.assertEqual({x["商品"] for x in impact["在制"]}, {p2})
        self.assertEqual({x["商品"] for x in impact["已售出或已完成"]}, {p3})


class WatermarkedFileTest(unittest.TestCase):
    def test_download_limits_count_expiry_and_supplier_scope(self):
        f = seed_full_flow()
        svc, v2, sup = f["svc"], f["v2"], f["supplier"]
        other = svc.register_supplier("别家工厂")
        lic = svc.issue_license(v2, ["商品生产"], "lic")["授权"]
        pid = svc.register_product("徽章", lic, sup["id"], "p")["商品"]
        grant = svc.grant_watermarked_file(pid, 2, "2026-12-31", "gf")["文件授权"]
        # 非订单供应商拿不到
        with self.assertRaises(ServiceError) as ctx:
            svc.download_file(grant, other["id"], "d0")
        self.assertEqual(ctx.exception.code, "越权访问")
        d1 = svc.download_file(grant, sup["id"], "d1")
        self.assertEqual(d1["已下载"], 1)
        self.assertTrue(d1["水印"])
        d2 = svc.download_file(grant, sup["id"], "d2")
        self.assertEqual(d2["已下载"], 2)
        with self.assertRaises(ServiceError) as ctx:
            svc.download_file(grant, sup["id"], "d3")
        self.assertEqual(ctx.exception.code, "次数超限")
        # 重复回调不重复计数
        replay = svc.download_file(grant, sup["id"], "d2")
        self.assertTrue(replay["重复"])
        self.assertEqual(svc.file_grants[grant]["已下载"], 2)

    def test_expired_grant_rejected(self):
        f = seed_full_flow()
        svc, v2, sup = f["svc"], f["v2"], f["supplier"]
        lic = svc.issue_license(v2, ["商品生产"], "lic")["授权"]
        pid = svc.register_product("徽章", lic, sup["id"], "p")["商品"]
        grant = svc.grant_watermarked_file(pid, 5, "2026-09-01", "gf")["文件授权"]
        # download_file 以服务器当日校验；直接构造过期事件验证规则
        with self.assertRaises(ServiceError) as ctx:
            svc._record_event("文件下载", {
                "文件授权": grant, "供应商": sup["id"], "日期": "2026-09-02"}, "dx")
        self.assertEqual(ctx.exception.code, "授权过期")


class SettlementTest(unittest.TestCase):
    def test_allocation_follows_confirmed_shares_and_is_idempotent(self):
        f = seed_full_flow()
        svc, v2, sup = f["svc"], f["v2"], f["supplier"]
        lic = svc.issue_license(v2, ["商品生产"], "lic")["授权"]
        pid = svc.register_product("徽章", lic, sup["id"], "p")["商品"]
        s1 = svc.generate_settlement(pid, "2026-09", 100.00, "set1")
        s2 = svc.generate_settlement(pid, "2026-09", 100.00, "set1")
        self.assertTrue(s2["重复"])
        alloc = s1["分配"]
        self.assertEqual(alloc[f["adult"]["id"]]["金额"], 40.00)
        self.assertEqual(alloc[f["student"]["id"]]["金额"], 60.00)
        self.assertEqual(round(sum(a["金额"] for a in alloc.values()), 2), 100.00)
        # 可追溯：结算钉住权属确认与授权版次
        record = next(s for s in svc.settlements.values() if s["周期"] == "2026-09")
        self.assertEqual(record["权属确认"], f["own"]["权属确认"])
        self.assertEqual(record["授权版次"], 1)

    def test_shares_must_cover_provenance_and_sum_one(self):
        f = seed_full_flow()
        svc, v2 = f["svc"], f["v2"]
        with self.assertRaises(ServiceError) as ctx:
            svc.confirm_ownership(v2, {f["adult"]["id"]: 1.0}, "bad1")
        self.assertEqual(ctx.exception.code, "比例错误")
        with self.assertRaises(ServiceError) as ctx:
            svc.confirm_ownership(v2, {f["adult"]["id"]: 0.5,
                                       f["student"]["id"]: 0.4}, "bad2")
        self.assertEqual(ctx.exception.code, "比例错误")


class ConsistencyTest(unittest.TestCase):
    def test_verify_clean_on_full_flow(self):
        f = seed_full_flow()
        svc, v2, sup = f["svc"], f["v2"], f["supplier"]
        lic = svc.issue_license(v2, ["商品生产"], "lic")["授权"]
        pid = svc.register_product("徽章", lic, sup["id"], "p")["商品"]
        svc.generate_settlement(pid, "2026-09", 100, "s")
        result = svc.verify_consistency()
        self.assertTrue(result["一致"], result["问题"])

    def test_duplicate_callbacks_keep_ledger_consistent(self):
        f = seed_full_flow()
        svc, batch = f["svc"], f["batch"]
        svc.generation_callback(batch["id"], True, "x", "cb-key")
        svc.withdraw_contribution(f["c1"], "r", "w")
        svc.withdraw_contribution(f["c1"], "r", "w")  # 幂等
        result = svc.verify_consistency()
        self.assertTrue(result["一致"], result["问题"])


class ConcurrencyTest(unittest.TestCase):
    def test_parallel_callbacks_and_grants_stay_consistent(self):
        f = seed_full_flow()
        svc, batch = f["svc"], f["batch"]
        errors = []

        def callback():
            try:
                svc.generation_callback(batch["id"], True, "x", "cb-key")
            except ServiceError as e:  # 成功回调后的其它键不会改写
                errors.append(e)

        def grant():
            try:
                svc.grant_purpose(f["c1"], "渠道宣传", "有效", "parallel-grant")
            except ServiceError as e:
                errors.append(e)

        threads = [threading.Thread(target=callback) for _ in range(8)]
        threads += [threading.Thread(target=grant) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(batch["回调次数"], 1)
        self.assertTrue(svc.verify_consistency()["一致"])


class HttpSmokeTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _post(self, path, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
            return resp.status, json.loads(resp.read())

    def test_health_and_rule_rejection(self):
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], SERVICE_ID)
        # 缺字段 -> 400
        status, body = self._post("/contributions", {"标题": "x"})
        self.assertEqual(status, 400)
        # 未知对象 -> 404
        status, body = self._post("/grants",
                                  {"贡献": "nope", "用途": "校内展示", "状态": "有效"})
        self.assertEqual(status, 404)

    def test_end_to_end_over_http_is_idempotent(self):
        _, brief = self._post("/briefs", {"标题": "季", "摘要": "s", "幂等键": "b"})
        _, brief2 = self._post("/briefs", {"标题": "季", "摘要": "s", "幂等键": "b"})
        self.assertEqual(brief["数据"]["id"], brief2["数据"]["id"])
        _, person = self._post("/contributors", {"姓名": "甲", "未成年": False})
        pid = person["数据"]["id"]
        _, contrib = self._post("/contributions", {
            "简报": brief["数据"]["id"], "贡献者": pid, "类型": "原始图像",
            "标题": "图", "幂等键": "c"})
        cid = contrib["数据"]["贡献"]
        status, body = self._post("/grants", {
            "贡献": cid, "用途": "校内展示", "状态": "有效"})
        self.assertEqual(status, 200)
        # 撤回后影响分析可查
        self._post("/withdraw", {"贡献": cid, "原因": "测试", "幂等键": "w"})
        status, impact = self._get(f"/impact/{cid}")
        self.assertEqual(status, 200)
        self.assertEqual(impact["状态"], "已撤回")


if __name__ == "__main__":
    unittest.main()
