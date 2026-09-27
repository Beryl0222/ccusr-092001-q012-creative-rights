"""端到端核对：身份、权属边界、来源图、回调幂等、撤回影响与结算追溯。"""

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

from app.api import make_handler
from app.engine import DomainError, RightsEngine, load_domain
from service import SERVICE_ID, check_config, health


def future(days=7):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


class HealthTest(unittest.TestCase):
    def test_identity(self):
        self.assertEqual(health(), {"status": "ok", "service": SERVICE_ID})

    def test_check_config(self):
        self.assertEqual(check_config(), [])


class ScenarioTest(unittest.TestCase):
    def setUp(self):
        self.engine = RightsEngine(domain=load_domain())
        self.brief = self.engine.create_brief("校园文创季", "帆布袋图案征集")

    def _adult_material(self, kind="原始图像", title="成人摄影"):
        cid = self.engine.register_contributor("成人贡献者", "安女士")["id"]
        mid = self.engine.contribute_material(
            self.brief["id"], cid, kind, title)["id"]
        return cid, mid

    def _grant_all(self, mid, by="本人", action="首次确认"):
        for scope in ["校内展示", "公益展览", "商品生产", "渠道宣传"]:
            self.engine.confirm_scope(mid, scope, action, by)

    # ------------------------------------------------------------- 权属边界

    def test_minor_requires_guardian_registration_and_consent(self):
        with self.assertRaises(DomainError):
            self.engine.register_contributor("未成年学生", "小明")
        minor = self.engine.register_contributor(
            "未成年学生", "小明", {"name": "明父", "contact": "13800000000"})
        mid = self.engine.contribute_material(
            self.brief["id"], minor["id"], "文字创意", "手绘小怪兽")["id"]
        # 学生本人确认无效
        with self.assertRaises(DomainError):
            self.engine.confirm_scope(mid, "校内展示", "首次确认", "小明")
        grant = self.engine.confirm_scope(mid, "校内展示", "监护确认", "明父")
        self.assertEqual(grant["grant"]["status"], "有效")
        design = self.engine.create_design(
            self.brief["id"], "小怪兽", "编辑甲",
            material_edges=[{"material_id": mid, "share": 1.0}])
        self.engine.request_review(design["id"])
        # 监护确认只授了校内展示，商业用途仍有缺口
        with self.assertRaises(DomainError) as ctx:
            self.engine.decide_review(design["id"], ["商品生产"], "版权运营")
        self.assertEqual(ctx.exception.code, "clearance_failed")
        # 补监护商业许可后通过
        self.engine.confirm_scope(mid, "商品生产", "监护确认", "明父")
        result = self.engine.decide_review(design["id"], ["商品生产"], "版权运营")
        self.assertEqual(result["status"], "已确认")

    def test_school_display_does_not_extend_to_commerce(self):
        school = self.engine.register_contributor("学校组织", "美术社团")["id"]
        mid = self.engine.contribute_material(
            self.brief["id"], school, "原始图像", "社团展板")["id"]
        # 校园展示许可
        self.engine.confirm_scope(mid, "校内展示", "契约确认", "校方代表")
        self.engine.confirm_scope(mid, "公益展览", "契约确认", "校方代表")
        design = self.engine.create_design(
            self.brief["id"], "展板改版", "编辑乙",
            material_edges=[{"material_id": mid, "share": 1.0}])
        self.engine.request_review(design["id"])
        # 校内展示的确权通过
        ok = self.engine.decide_review(design["id"], ["校内展示"], "版权运营")
        self.assertEqual(ok["status"], "已确认")
        # 同一设计不能凭校园许可扩展为商用
        self.engine.request_review(design["id"])
        with self.assertRaises(DomainError) as ctx:
            self.engine.decide_review(design["id"], ["商品生产"], "版权运营")
        self.assertIn("clearance_failed", ctx.exception.code)
        # 单独签订商业契约后才放行
        self.engine.confirm_scope(mid, "商品生产", "契约确认", "校方代表")
        again = self.engine.decide_review(design["id"], ["商品生产"], "版权运营")
        self.assertEqual(again["status"], "已确认")

    # ------------------------------------------------------------- 来源图

    def test_merge_records_parents_and_weights_propagate(self):
        ca, ma = self._adult_material("原始图像", "摄影底图")
        cm, mm = self._adult_material("文字创意", "书法字")
        self._grant_all(ma)
        self._grant_all(mm)
        # 机器生成批次：摄影 0.6 + 书法 0.4
        batch = self.engine.create_batch(
            self.brief["id"], "国潮帆布袋",
            [{"material_id": ma, "share": 0.6},
             {"material_id": mm, "share": 0.4}], "img-model-3", "操作员丁")
        self.assertEqual(len(batch["items"]), 3)
        d1 = self.engine.create_design(
            self.brief["id"], "机器候选定稿", "操作员丁",
            batch_edges=[{"batch_item_id": batch["items"][0]["item_id"], "share": 1.0}])
        # 人工修改：父版本 0.8 + 直接再用摄影 0.2
        d2 = self.engine.create_design(
            self.brief["id"], "人工修改版", "设计师戊",
            parent_edges=[{"design_id": d1["id"], "share": 0.8}],
            material_edges=[{"material_id": ma, "share": 0.2}])
        prov = self.engine.provenance(d2["id"])
        weights = {m["material_id"]: m["weight"] for m in prov["materials"]}
        self.assertTrue(prov["machine_assisted"])
        self.assertAlmostEqual(weights[ma], 0.68, places=4)   # 0.8*0.6 + 0.2
        self.assertAlmostEqual(weights[mm], 0.32, places=4)   # 0.8*0.4
        self.assertAlmostEqual(prov["weight_sum"], 1.0, places=4)
        # 来源边完整记录了父版本与所用素材
        edges = [e for e in self.engine._store["design_edges"]
                 if e["design_id"] == d2["id"]]
        self.assertEqual({e["kind"] for e in edges}, {"parent", "material"})

    def test_design_shares_must_balance(self):
        _ca, ma = self._adult_material()
        with self.assertRaises(DomainError):
            self.engine.create_design(
                self.brief["id"], "比例不平", "编辑",
                material_edges=[{"material_id": ma, "share": 0.5}])

    # ------------------------------------------------------------- 回调幂等与驳回重提

    def _confirmed_design(self):
        ca, ma = self._adult_material()
        cm, mm = self._adult_material("文字创意", "书法字")
        self._grant_all(ma)
        self._grant_all(mm)
        d = self.engine.create_design(
            self.brief["id"], "成品", "设计师",
            material_edges=[{"material_id": ma, "share": 0.6},
                            {"material_id": mm, "share": 0.4}])
        self.engine.request_review(d["id"])
        self.engine.decide_review(d["id"], ["商品生产", "渠道宣传"], "版权运营")
        return d, ma, mm

    def test_duplicate_sample_callbacks_and_rejection_rounds(self):
        d, _ma, _mm = self._confirmed_design()
        sample = self.engine.create_sample(d["id"], "供应商鑫", "380g 帆布")
        first = self.engine.submit_sample_round(
            sample["id"], "proof-v1.png", callback_id="cb-1", idem="client-1")
        # 完全相同的客户端重试与回调重复投递
        replay = self.engine.submit_sample_round(
            sample["id"], "proof-v1.png", callback_id="cb-1", idem="client-1")
        self.assertTrue(replay.get("idempotent_replay"))
        self.assertEqual(len(self.engine._store["samples"][sample["id"]]["rounds"]), 1)
        # 驳回后重提，轮次递增
        self.engine.verify_sample(sample["id"], False, "品控", "色差过大")
        second = self.engine.submit_sample_round(
            sample["id"], "proof-v2.png", callback_id="cb-2")
        self.assertEqual(second["round"], 2)
        verdict = self.engine.verify_sample(sample["id"], True, "品控")
        self.assertEqual(verdict["status"], "验收通过")
        sealed = self.engine.seal_sample(sample["id"])
        self.assertEqual(sealed["status"], "已封样")
        # 封样后不能再提交
        with self.assertRaises(DomainError):
            self.engine.submit_sample_round(sample["id"], "proof-v3.png")

    # ------------------------------------------------------------- 授权版本与受控下载

    def test_license_versions_and_download_limits(self):
        d, _ma, _mm = self._confirmed_design()
        product = self.engine.create_product(
            "国潮帆布袋", [{"design_id": d["id"], "share": 1.0}])
        v1 = self.engine.issue_license(
            product["id"], ["商品生产"], ["自营店"], terms="初版")
        self.assertEqual(v1["version"], 1)
        self.engine.activate_product(product["id"])
        order = self.engine.create_order(product["id"], "供应商鑫", 500)
        # 到期时间非法（过去）被拒
        with self.assertRaises(DomainError):
            self.engine.issue_download_grant(
                order["id"], "水印印刷稿.pdf", 3, "2000-01-01T00:00:00+00:00")
        grant = self.engine.issue_download_grant(
            order["id"], "水印印刷稿.pdf", 2, future(7))
        self.assertTrue(grant["watermarked"])
        self.assertIn(order["id"], json.dumps(grant["watermark"], ensure_ascii=False))
        f1 = self.engine.fetch_file(grant["id"])
        self.engine.fetch_file(grant["id"])  # 第二次
        with self.assertRaises(DomainError) as ctx:
            self.engine.fetch_file(grant["id"])  # 第三次超限
        self.assertEqual(ctx.exception.code, "grant_exhausted")
        self.assertEqual(self.engine._store["download_grants"][grant["id"]]["downloads"], 2)
        # 授权版本更新：扩展渠道，旧版留痕
        v2 = self.engine.update_license(
            product["id"], "范围扩展", ["商品生产", "渠道宣传"],
            ["自营店", "经销商"], terms="扩渠道")
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["supersedes"], 1)
        history = self.engine._store["license_versions"][product["id"]]
        self.assertEqual([h["version"] for h in history], [1, 2])
        self.assertNotEqual(history[0]["status"], "有效")
        # 扩展到未授权的范围必须被拒
        withdrawn_pid = product["id"]
        # 直接构造一个无渠道宣传素材授权的商品验证缺口
        ca, ma = self._adult_material("原始图像", "仅生产素材")
        self.engine.confirm_scope(ma, "商品生产", "首次确认", "本人")
        d2 = self.engine.create_design(
            self.brief["id"], "窄授权设计", "设计师",
            material_edges=[{"material_id": ma, "share": 1.0}])
        self.engine.request_review(d2["id"])
        self.engine.decide_review(d2["id"], ["商品生产"], "版权运营")
        p2 = self.engine.create_product("窄授权商品", [{"design_id": d2["id"], "share": 1.0}])
        self.engine.issue_license(p2["id"], ["商品生产"], ["自营店"])
        with self.assertRaises(DomainError):
            self.engine.update_license(
                p2["id"], "范围扩展", ["商品生产", "渠道宣传"], ["自营店"])

    def test_grant_expiry_blocks_fetch(self):
        d, _ma, _mm = self._confirmed_design()
        product = self.engine.create_product(
            "限期商品", [{"design_id": d["id"], "share": 1.0}])
        self.engine.issue_license(product["id"], ["商品生产"], ["自营店"])
        order = self.engine.create_order(product["id"], "供应商鑫", 100)
        grant = self.engine.issue_download_grant(
            order["id"], "水印稿.pdf", 3, future(7))
        self.engine._store["download_grants"][grant["id"]]["expires_at"] = \
            "2000-01-01T00:00:00+00:00"
        with self.assertRaises(DomainError) as ctx:
            self.engine.fetch_file(grant["id"])
        self.assertEqual(ctx.exception.code, "grant_expired")
        self.assertEqual(
            self.engine._store["download_grants"][grant["id"]]["status"], "已过期")

    # ------------------------------------------------------------- 撤回三分影响 + 结算

    def _commercial_product(self):
        d, ma, mm = self._confirmed_design()
        product = self.engine.create_product(
            "帆布袋", [{"design_id": d["id"], "share": 1.0}])
        self.engine.issue_license(product["id"], ["商品生产"], ["自营店"])
        self.engine.activate_product(product["id"])
        order = self.engine.create_order(product["id"], "供应商鑫", 500)
        grant = self.engine.issue_download_grant(
            order["id"], "水印稿.pdf", 3, future(7))
        unmade = self.engine.create_production_batch(product["id"], 300, "未排产")
        in_prog = self.engine.create_production_batch(product["id"], 200, "生产中")
        self.engine.record_sale(product["id"], 10, "自营店", order_ref="SO-1")
        return d, product, order, grant, unmade, in_prog, ma, mm

    def test_withdraw_impact_split_never_deletes(self):
        d, product, order, grant, unmade, in_prog, _ma, mm = self._commercial_product()
        impact = self.engine.impact_analysis(mm)
        self.assertIn(unmade["id"], impact["unmade_batches"])
        self.assertIn(in_prog["id"], impact["inprogress_batches"])
        self.assertEqual(impact["sold_qty"], 10)
        events_before = len(self.engine.events())

        result = self.engine.withdraw_material(mm, "作者投诉署名缺失")
        self.assertEqual(result["status"], "已撤回")
        # 三类影响分别给出，记录一条不删
        self.assertIn(unmade["id"], result["impact"]["unmade_batches"])
        self.assertIn(in_prog["id"], result["impact"]["inprogress_batches"])
        self.assertEqual(result["impact"]["sold_qty"], 10)
        self.assertEqual(
            self.engine._store["products"][product["id"]]["status"], "召回冻结")
        self.assertEqual(
            self.engine._store["download_grants"][grant["id"]]["status"], "已废止")
        self.assertEqual(self.engine._store["designs"][d["id"]]["status"], "已撤回")
        # 销售与批次记录仍在
        self.assertEqual(len(self.engine._store["sales"]), 1)
        self.assertEqual(
            self.engine._store["production_batches"][in_prog["id"]]["status"], "生产中")
        self.assertGreater(len(self.engine.events()), events_before)
        # 冻结期不能继续销售、下单、放行文件
        with self.assertRaises(DomainError):
            self.engine.record_sale(product["id"], 1, "自营店")
        with self.assertRaises(DomainError):
            self.engine.create_order(product["id"], "供应商乙", 10)
        with self.assertRaises(DomainError):
            self.engine.fetch_file(grant["id"])
        # 撤回后不能再结算
        with self.assertRaises(DomainError):
            self.engine.create_settlement_run(product["id"], "2026-09", 1000)

    def test_dispute_freeze_then_revise_recovers(self):
        d, product, order, grant, _unmade, _inprog, _ma, mm = self._commercial_product()
        self.engine.open_dispute(mm, "第三方主张权利")
        self.assertEqual(
            self.engine._store["materials"][mm]["status"], "争议中")
        self.assertEqual(
            self.engine._store["designs"][d["id"]]["status"], "争议冻结")
        # 争议解除后设计需重新确权；这里用干净素材出一个新版本替换恢复
        self.engine.resolve_dispute(mm, "主张不成立")
        _ca, clean = self._adult_material("原始图像", "替代素材")
        self._grant_all(clean)
        d_new = self.engine.create_design(
            self.brief["id"], "替代版本", "设计师",
            material_edges=[{"material_id": clean, "share": 1.0}])
        self.engine.request_review(d_new["id"])
        self.engine.decide_review(d_new["id"], ["商品生产"], "版权运营")
        new_license = self.engine.revise_product_design(
            product["id"], [{"design_id": d_new["id"], "share": 1.0}],
            ["商品生产"], "替换争议素材")
        self.assertEqual(new_license["version"], 2)
        self.engine.activate_product(product["id"])
        self.assertEqual(
            self.engine._store["products"][product["id"]]["status"], "在售")

    def test_settlement_traceable_split_and_callback_idempotency(self):
        _d, product, _order, _grant, _u, _p, ma, mm = self._commercial_product()
        run = self.engine.create_settlement_run(product["id"], "2026-09", 1000)
        self.assertEqual(run["license_version"], 1)
        amounts = {e["contributor_id"]: e["amount"] for e in run["entries"]}
        # 0.6/0.4 → 600/400，分毫不差且可追溯到素材与授权快照
        self.assertEqual(sum(amounts.values()), 1000.0)
        self.assertEqual(len(amounts), 2)
        weights = {e["contributor_id"]: e["weight"] for e in run["entries"]}
        contributors = {c["id"]: c for c in self.engine._store["contributors"].values()}
        owner_ma = self.engine._store["materials"][ma]["contributor_id"]
        owner_mm = self.engine._store["materials"][mm]["contributor_id"]
        self.assertAlmostEqual(weights[owner_ma], 0.6, places=4)
        self.assertAlmostEqual(weights[owner_mm], 0.4, places=4)
        for entry in run["entries"]:
            self.assertEqual(entry["snapshot_hash"], run["snapshot_hash"])
        # 未锁定拒绝支付回调
        with self.assertRaises(DomainError):
            self.engine.payment_callback(run["id"], "pay-1")
        self.engine.lock_settlement(run["id"])
        paid = self.engine.payment_callback(run["id"], "pay-1")
        self.assertEqual(paid["status"], "已分配")
        # 支付网关重复回调
        replay = self.engine.payment_callback(run["id"], "pay-1", idem="cb-retry")
        self.assertTrue(replay.get("unchanged"))
        # 不同 callback_id 同幂等键也不重复分配
        again = self.engine.payment_callback(run["id"], "pay-1-dup")
        self.assertTrue(again.get("unchanged"))

    def test_settlement_blocks_when_provenance_drifts_after_license_update(self):
        d, product, *_ = self._commercial_product()
        # 先开一笔待结算
        run1 = self.engine.create_settlement_run(product["id"], "2026-08", 500)
        # 用新设计改版商品 → 现行授权快照变化
        _ca, clean = self._adult_material("原始图像", "新季素材")
        self._grant_all(clean)
        d_new = self.engine.create_design(
            self.brief["id"], "新季版本", "设计师",
            material_edges=[{"material_id": clean, "share": 1.0}])
        self.engine.request_review(d_new["id"])
        self.engine.decide_review(d_new["id"], ["商品生产"], "版权运营")
        self.engine.revise_product_design(
            product["id"], [{"design_id": d_new["id"], "share": 1.0}],
            ["商品生产"], "换季改版")
        report = self.engine.consistency_report()
        flagged = [p for p in report["problems"]
                   if p.get("run_id") == run1["id"]]
        self.assertTrue(flagged, "待结算批次漂移必须被一致性检查发现")
        # 新批次按新授权版本结算；旧批次锁定后不再被标记
        self.engine.lock_settlement(run1["id"])
        run2 = self.engine.create_settlement_run(product["id"], "2026-09", 800)
        self.assertEqual(run2["snapshot_hash"],
                         self.engine._store["licenses"][product["id"]]["snapshot_hash"])
        self.assertTrue(self.engine.consistency_report()["ok"])

    def test_partial_scope_withdraw_leaves_unrelated_design_intact(self):
        # 素材同时用于"仅校内展示"设计与"商业"设计，撤回商业许可只冻结后者
        ca, ma = self._adult_material()
        self.engine.confirm_scope(ma, "校内展示", "首次确认", "本人")
        self.engine.confirm_scope(ma, "商品生产", "首次确认", "本人")
        d_school = self.engine.create_design(
            self.brief["id"], "展板用图", "社团",
            material_edges=[{"material_id": ma, "share": 1.0}])
        self.engine.request_review(d_school["id"])
        self.engine.decide_review(d_school["id"], ["校内展示"], "版权运营")
        d_shop = self.engine.create_design(
            self.brief["id"], "商品用图", "运营",
            material_edges=[{"material_id": ma, "share": 1.0}])
        self.engine.request_review(d_shop["id"])
        self.engine.decide_review(d_shop["id"], ["商品生产"], "版权运营")
        self.engine.withdraw_material(ma, "商业合作终止", scope="商品生产")
        self.assertEqual(
            self.engine._store["designs"][d_shop["id"]]["status"], "受限冻结")
        self.assertEqual(
            self.engine._store["designs"][d_school["id"]]["status"], "已确认")

    def test_duplicate_confirm_is_idempotent(self):
        _ca, ma = self._adult_material()
        g1 = self.engine.confirm_scope(ma, "商品生产", "首次确认", "本人")
        g2 = self.engine.confirm_scope(ma, "商品生产", "首次确认", "本人")
        self.assertTrue(g2.get("unchanged"))
        self.assertEqual(
            len(self.engine._store["materials"][ma]["grants"]), 1)


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = RightsEngine(domain=load_domain())
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.engine))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _post(self, path, body, key=None):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        req = Request(f"http://127.0.0.1:{self.port}{path}",
                      data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except Exception as exc:  # HTTPError
            payload = json.loads(exc.read())
            return exc.code, payload

    def _get(self, path):
        with urlopen(f"http://127.0.0.1:{self.port}{path}") as resp:
            return json.loads(resp.read())

    def test_full_flow_over_http_with_idempotency(self):
        health = self._get("/health")
        self.assertEqual(health["service"], SERVICE_ID)
        s1, brief = self._post("/briefs", {"title": "HTTP 简报"}, key="brief-1")
        self.assertEqual(s1, 200)
        # 重复提交（幂等键）只创建一次
        _, replay = self._post("/briefs", {"title": "HTTP 简报"}, key="brief-1")
        self.assertEqual(replay["id"], brief["id"])
        # 未成年人缺监护人 → 422
        status, err = self._post("/contributors",
                                 {"ctype": "未成年学生", "name": "小红"})
        self.assertEqual(status, 422)
        self.assertEqual(err["error"], "domain_rule")
        status, minor = self._post("/contributors", {
            "ctype": "未成年学生", "name": "小红",
            "guardian": {"name": "红父", "contact": "139"}})
        self.assertEqual(status, 200)
        _, mat = self._post("/materials", {
            "brief_id": brief["id"], "contributor_id": minor["id"],
            "kind": "文字创意", "title": "小诗"})
        # 缺字段 → 400
        status, err = self._post(f"/materials/{mat['id']}/confirm",
                                 {"scope": "校内展示"})
        self.assertEqual(status, 400)
        # 无监护确认 → 422
        status, err = self._post(f"/materials/{mat['id']}/confirm",
                                 {"scope": "校内展示", "action": "首次确认", "by": "小红"})
        self.assertEqual(status, 422)
        status, _ = self._post(f"/materials/{mat['id']}/confirm", {
            "scope": "校内展示", "action": "监护确认", "by": "红父"})
        self.assertEqual(status, 200)
        report = self._get("/consistency")
        self.assertTrue(report["ok"])


if __name__ == "__main__":
    unittest.main()
