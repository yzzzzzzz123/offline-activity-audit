from __future__ import annotations

import unittest
from unittest.mock import patch

from audit_core.display import _determine_store_match
from audit_core.location_resolution import (
    BAIDU_MAPS_MCP_ENDPOINT,
    NEARBY_PASS_METERS,
    UNRELATED_MIN_METERS,
    StoreLocationResolver,
    _baidu_maps_mcp_url,
    baidu_maps_mcp_lookup_from_environment,
    city_hint_from_values,
    classify_poi_relationship,
    load_location_registry,
    select_compatible_poi_pair,
    select_unique_poi,
)


def _search_response(
    *,
    query: str,
    name: str,
    address: str,
    location: str,
    poi_id: str,
) -> dict:
    longitude, latitude = (float(value) for value in location.split(",", 1))
    return {
        "query": {"query": query, "region": "东莞市"},
        "tool": "map_search_places",
        "payloads": [
            {
                "status": 0,
                "message": "ok",
                "results": [
                    {
                        "uid": poi_id,
                        "name": name,
                        "location": {
                            "lat": latitude,
                            "lng": longitude,
                        },
                        "province": "广东省",
                        "city": "东莞市",
                        "area": "",
                        "address": address,
                    }
                ]
            }
        ],
    }


class _NeverCalledResolver:
    def resolve(self, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError(f"名称直接一致时不应调用地点MCP：{kwargs}")


class LocationResolutionTests(unittest.TestCase):
    def test_exact_visible_store_name_does_not_call_mcp(self) -> None:
        store_match, basis, resolution = _determine_store_match(
            "示例生活超市（横沥店）",
            None,
            "东莞市·示例生活超市（横沥店）",
            _NeverCalledResolver(),
        )
        self.assertEqual(store_match, "exact")
        self.assertIn("无需调用地图MCP", basis)
        self.assertEqual(resolution["status"], "not_needed")
        self.assertEqual(resolution["confidence"], "not_applicable")
        self.assertFalse(resolution["mcp_attempted"])

    def test_known_mall_and_store_pair_uses_audited_authoritative_fallback(self) -> None:
        resolver = StoreLocationResolver(
            mcp_lookup=None,
            registry=load_location_registry(),
        )
        store_match, basis, resolution = _determine_store_match(
            "挺拇指生活超市（横沥店）",
            None,
            "东莞市·南铭购物乐园",
            resolver,
        )
        self.assertEqual(store_match, "compatible")
        self.assertEqual(resolution["status"], "parent_child")
        self.assertEqual(resolution["confidence"], "high")
        self.assertEqual(resolution["provider"], "authoritative_registry")
        self.assertEqual(resolution["mcp_status"], "not_configured")
        self.assertFalse(resolution["mcp_attempted"])
        self.assertIn("中山中路88号", basis)
        self.assertIn("东莞市市场监督管理局", "；".join(resolution["evidence"]))

    def test_nearby_hotel_and_shopping_center_pair_is_high_confidence(self) -> None:
        resolver = StoreLocationResolver(
            mcp_lookup=None,
            registry=load_location_registry(),
        )
        store_match, basis, resolution = _determine_store_match(
            "润家连锁超市（美联购物中心店）",
            None,
            "深圳市宝安区·深圳紫云快捷宾馆",
            resolver,
        )
        self.assertEqual(store_match, "compatible")
        self.assertEqual(resolution["status"], "nearby")
        self.assertEqual(resolution["confidence"], "high")
        self.assertEqual(resolution["provider"], "authoritative_registry")
        self.assertEqual(resolution["distance_meters"], 65.4)
        self.assertIn("不超过100米", basis)

    def test_mcp_same_building_result_passes_as_parent_child(self) -> None:
        calls: list[list[dict[str, str]]] = []

        def lookup(queries: list[dict[str, str]]) -> list[dict]:
            calls.append(queries)
            return [
                _search_response(
                    query=queries[0]["query"],
                    name="挺拇指生活超市（横沥店）",
                    address="横沥镇中山中路88号南铭购物乐园二楼",
                    location="113.950000,22.990000",
                    poi_id="store-1",
                ),
                _search_response(
                    query=queries[1]["query"],
                    name="南铭购物乐园",
                    address="横沥镇中山中路88号",
                    location="113.950030,22.990020",
                    poi_id="mall-1",
                ),
            ]

        resolver = StoreLocationResolver(mcp_lookup=lookup, registry={"entries": []})
        resolution = resolver.resolve(
            contract_name="挺拇指生活超市（横沥店）",
            contract_address=None,
            visible_location="东莞市·南铭购物乐园",
            city_hint="东莞市",
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(resolution["status"], "parent_child")
        self.assertEqual(resolution["confidence"], "high")
        self.assertEqual(
            resolution["provider"],
            "baidu_maps_mcp",
        )
        self.assertEqual(resolution["coordinate_system"], "BD09LL")
        self.assertTrue(resolution["mcp_attempted"])
        self.assertLessEqual(resolution["distance_meters"], NEARBY_PASS_METERS)
        self.assertIn("map_search_places", "；".join(resolution["evidence"]))
        self.assertIn("BD09LL", "；".join(resolution["evidence"]))
        self.assertEqual(
            resolution["contract_poi"]["address"],
            "广东省东莞市横沥镇中山中路88号南铭购物乐园二楼",
        )

    def test_non_unique_contract_search_uses_relevant_nearby_candidate(self) -> None:
        def lookup(queries: list[dict[str, str]]) -> list[dict]:
            contract_response = _search_response(
                query=queries[0]["query"],
                name="九龙购物中心(宝安沙井店)",
                address="深圳市宝安区沙井西环路2108号",
                location="113.8081625,22.7363834",
                poi_id="shopping-center",
            )
            hotel_response = _search_response(
                query=queries[0]["query"],
                name="九龙酒店(深圳沙井店)",
                address="深圳市宝安区沙井环镇路145号",
                location="113.8183259,22.7448739",
                poi_id="hotel",
            )
            contract_response["payloads"][0]["results"].extend(
                hotel_response["payloads"][0]["results"]
            )
            return [
                contract_response,
                _search_response(
                    query=queries[1]["query"],
                    name="九龙购物中心(宝安沙井店)",
                    address="深圳市宝安区沙井西环路2108号",
                    location="113.8081625,22.7363834",
                    poi_id="shopping-center",
                ),
            ]

        resolver = StoreLocationResolver(mcp_lookup=lookup, registry={"entries": []})
        resolution = resolver.resolve(
            contract_name="沙井九龙",
            contract_address=None,
            visible_location="深圳市·九龙购物中心（宝安沙井店）",
            city_hint="深圳市",
        )
        self.assertEqual(resolution["status"], "same_place")
        self.assertEqual(resolution["confidence"], "high")
        self.assertEqual(resolution["contract_poi"]["poi_id"], "shopping-center")
        self.assertEqual(resolution["watermark_poi"]["poi_id"], "shopping-center")
        self.assertIn("不要求两次搜索结果各自唯一", resolution["basis"])
        self.assertIn("返回2个候选", "；".join(resolution["evidence"]))

    def test_non_unique_business_type_aliases_can_pair_at_same_poi(self) -> None:
        pair, basis = select_compatible_poi_pair(
            contract_query="百佳华百货（公明店）",
            contract_candidates=[
                {
                    "name": "百佳华商场(公明店)",
                    "address": "广东省深圳市光明区长春南路西1号",
                    "poi_id": "gongming",
                    "longitude": 113.9006979,
                    "latitude": 22.7855834,
                },
                {
                    "name": "百佳华佳漾邻(松岗店)",
                    "address": "广东省深圳市宝安区松明大道293号",
                    "poi_id": "songgang",
                    "longitude": 113.8538565,
                    "latitude": 22.7737267,
                },
            ],
            watermark_query="深圳市·百佳华佳漾汇（公明店）",
            watermark_candidates=[
                {
                    "name": "百佳华商场(公明店)",
                    "address": "广东省深圳市光明区长春南路西1号",
                    "poi_id": "gongming",
                    "longitude": 113.9006979,
                    "latitude": 22.7855834,
                },
                {
                    "name": "佳漾汇(新桥店)",
                    "address": "广东省深圳市宝安区沙井中心路111号",
                    "poi_id": "xinqiao",
                    "longitude": 113.8352235,
                    "latitude": 22.7391355,
                },
            ],
        )
        self.assertIsNotNone(pair)
        assert pair is not None
        self.assertEqual(pair["contract_poi"]["poi_id"], "gongming")
        self.assertEqual(pair["watermark_poi"]["poi_id"], "gongming")
        self.assertEqual(pair["relationship"], "same_place")
        self.assertIn("名称相关性", basis)

    def test_district_prefix_alias_can_pair_at_same_poi(self) -> None:
        shared_poi = {
            "name": "宜多百货广场(炭步店)",
            "address": "广东省广州市花都区炭步镇南街路128号",
            "poi_id": "huadu-yiduo",
            "longitude": 113.1131630,
            "latitude": 23.3382883,
        }
        pair, _ = select_compatible_poi_pair(
            contract_query="花都宜多",
            contract_candidates=[
                shared_poi,
                {
                    "name": "家宜多百货(友田店)",
                    "address": "广东省广州市花都区狮岭镇盘古中路5号",
                    "poi_id": "other-yiduo",
                    "longitude": 113.1617372,
                    "latitude": 23.4682795,
                },
            ],
            watermark_query="广东省广州市花都区炭步镇·炭步宜多百货",
            watermark_candidates=[shared_poi],
            watermark_anchor=shared_poi,
        )

        self.assertIsNotNone(pair)
        assert pair is not None
        self.assertEqual(pair["contract_poi"]["poi_id"], "huadu-yiduo")
        self.assertEqual(pair["watermark_poi"]["poi_id"], "huadu-yiduo")
        self.assertEqual(pair["relationship"], "same_place")

    def test_candidate_pair_preserves_independently_selected_anchor(self) -> None:
        contract_anchor = {
            "name": "家家欣商场(赤岗路店)",
            "address": "广东省东莞市赤岗路与翠湖路交叉口东南60米",
            "poi_id": "jiajiaxin",
            "longitude": 113.7107521,
            "latitude": 22.8473032,
        }
        pair, basis = select_compatible_poi_pair(
            contract_query="虎门家家欣",
            contract_candidates=[
                contract_anchor,
                {
                    "name": "摄影部(家家欣商场店)",
                    "address": "广东省东莞市虎门镇赤岗家家欣一楼",
                    "poi_id": "photo-shop",
                    "longitude": 113.7103803,
                    "latitude": 22.8473526,
                },
            ],
            watermark_query="东莞市·赤岗家家欣商场",
            watermark_candidates=[
                dict(contract_anchor),
                {
                    "name": "家家欣商场(赤岗路店)-立体停车场",
                    "address": "广东省东莞市赤岗路与翠湖路交叉口东南60米",
                    "poi_id": "parking",
                    "longitude": 113.7103768,
                    "latitude": 22.8471956,
                },
            ],
            contract_anchor=contract_anchor,
        )
        self.assertIsNotNone(pair)
        assert pair is not None
        self.assertIs(pair["contract_poi"], contract_anchor)
        self.assertEqual(pair["watermark_poi"]["poi_id"], "jiajiaxin")
        self.assertIn("保留已唯一定位侧", basis)

    def test_weak_child_poi_selection_does_not_override_main_store_pair(self) -> None:
        def lookup(queries: list[dict[str, str]]) -> list[dict]:
            contract_response = _search_response(
                query=queries[0]["query"],
                name="家家欣商场(赤岗路店)",
                address="东莞市赤岗路与翠湖路交叉口东南60米",
                location="113.7107521,22.8473032",
                poi_id="jiajiaxin",
            )
            child_response = _search_response(
                query=queries[0]["query"],
                name="摄影部(家家欣商场店)",
                address="东莞市虎门镇赤岗家家欣一楼",
                location="113.7103803,22.8473526",
                poi_id="photo-shop",
            )
            contract_response["payloads"][0]["results"].extend(
                child_response["payloads"][0]["results"]
            )
            watermark_response = _search_response(
                query=queries[1]["query"],
                name="家家欣商场(赤岗路店)",
                address="东莞市赤岗路与翠湖路交叉口东南60米",
                location="113.7107521,22.8473032",
                poi_id="jiajiaxin",
            )
            parking_response = _search_response(
                query=queries[1]["query"],
                name="家家欣商场(赤岗路店)-立体停车场",
                address="东莞市赤岗路与翠湖路交叉口东南60米",
                location="113.7103768,22.8471956",
                poi_id="parking",
            )
            watermark_response["payloads"][0]["results"].extend(
                parking_response["payloads"][0]["results"]
            )
            return [contract_response, watermark_response]

        resolution = StoreLocationResolver(
            mcp_lookup=lookup,
            registry={"entries": []},
        ).resolve(
            contract_name="虎门家家欣",
            contract_address=None,
            visible_location="东莞市·赤岗家家欣商场",
            city_hint="东莞市",
        )
        self.assertEqual(resolution["status"], "same_place")
        self.assertEqual(resolution["contract_poi"]["poi_id"], "jiajiaxin")
        self.assertEqual(resolution["watermark_poi"]["poi_id"], "jiajiaxin")

    def test_nearby_pair_below_name_relevance_threshold_stays_ambiguous(self) -> None:
        def lookup(queries: list[dict[str, str]]) -> list[dict]:
            contract_response = _search_response(
                query=queries[0]["query"],
                name="完全无关地点甲",
                address="深圳市宝安区测试路1号",
                location="113.80816,22.73638",
                poi_id="unrelated-a",
            )
            second_response = _search_response(
                query=queries[0]["query"],
                name="完全无关地点乙",
                address="深圳市宝安区测试路2号",
                location="113.80817,22.73639",
                poi_id="unrelated-b",
            )
            contract_response["payloads"][0]["results"].extend(
                second_response["payloads"][0]["results"]
            )
            return [
                contract_response,
                _search_response(
                    query=queries[1]["query"],
                    name="九龙购物中心(宝安沙井店)",
                    address="深圳市宝安区沙井西环路2108号",
                    location="113.8081625,22.7363834",
                    poi_id="shopping-center",
                ),
            ]

        resolution = StoreLocationResolver(
            mcp_lookup=lookup,
            registry={"entries": []},
        ).resolve(
            contract_name="沙井九龙",
            contract_address=None,
            visible_location="深圳市·九龙购物中心（宝安沙井店）",
            city_hint="深圳市",
        )
        self.assertEqual(resolution["status"], "ambiguous")
        self.assertEqual(resolution["confidence"], "low")
        self.assertIsNone(resolution["contract_poi"])
        self.assertIn("候选配对未通过", resolution["basis"])

    def test_ambiguous_baidu_candidates_are_recorded_before_registry_fallback(self) -> None:
        def lookup(queries: list[dict[str, str]]) -> list[dict]:
            contract_response = _search_response(
                query=queries[0]["query"],
                name="Tingmuzhi Lifestyle Supermarket",
                address="",
                location="113.95,22.99",
                poi_id="store-1",
            )
            second = _search_response(
                query=queries[0]["query"],
                name="Ting Thumb Lifestyle Supermarket",
                address="",
                location="113.99,23.02",
                poi_id="store-2",
            )
            contract_response["payloads"][0]["results"].extend(
                second["payloads"][0]["results"]
            )
            return [
                contract_response,
                _search_response(
                    query=queries[1]["query"],
                    name="Nanming Shopping Paradise",
                    address="",
                    location="113.970968,23.0149396",
                    poi_id="mall-1",
                ),
            ]

        resolver = StoreLocationResolver(
            mcp_lookup=lookup,
            registry=load_location_registry(),
        )
        resolution = resolver.resolve(
            contract_name="挺拇指生活超市（横沥店）",
            contract_address=None,
            visible_location="东莞市·南铭购物乐园",
            city_hint="东莞市",
        )
        evidence = "；".join(resolution["evidence"])
        self.assertEqual(resolution["status"], "parent_child")
        self.assertEqual(resolution["provider"], "authoritative_registry")
        self.assertEqual(resolution["mcp_status"], "ambiguous")
        self.assertTrue(resolution["mcp_attempted"])
        self.assertIn("返回2个候选", evidence)
        self.assertIn("BD09LL", evidence)
        self.assertIn("东莞市市场监督管理局", evidence)

    def test_nearby_unique_pois_pass_at_or_below_100_meters(self) -> None:
        relationship, distance, basis = classify_poi_relationship(
            {
                "name": "合同门店",
                "address": "东莞市测试路1号",
                "poi_id": "contract",
                "longitude": 113.95,
                "latitude": 22.99,
            },
            {
                "name": "照片地点",
                "address": "东莞市测试路2号",
                "poi_id": "watermark",
                "longitude": 113.9504,
                "latitude": 22.9902,
            },
        )
        self.assertEqual(relationship, "nearby")
        self.assertIsNotNone(distance)
        self.assertLessEqual(distance, NEARBY_PASS_METERS)
        self.assertIn("100米", basis)

    def test_gray_zone_is_manual_review_not_error(self) -> None:
        relationship, distance, _ = classify_poi_relationship(
            {
                "name": "合同门店",
                "address": "甲路1号",
                "poi_id": "contract",
                "longitude": 113.95,
                "latitude": 22.99,
            },
            {
                "name": "照片地点",
                "address": "乙路8号",
                "poi_id": "watermark",
                "longitude": 113.9515,
                "latitude": 22.9904,
            },
        )
        self.assertEqual(relationship, "ambiguous")
        self.assertIsNotNone(distance)
        self.assertGreater(distance, NEARBY_PASS_METERS)
        self.assertLess(distance, UNRELATED_MIN_METERS)

    def test_uniquely_resolved_distant_pois_have_low_location_confidence(self) -> None:
        relationship, distance, basis = classify_poi_relationship(
            {
                "name": "合同门店",
                "address": "甲路1号",
                "poi_id": "contract",
                "longitude": 113.95,
                "latitude": 22.99,
            },
            {
                "name": "照片地点",
                "address": "乙路8号",
                "poi_id": "watermark",
                "longitude": 113.96,
                "latitude": 23.0,
            },
        )
        self.assertEqual(relationship, "unrelated")
        self.assertIsNotNone(distance)
        self.assertGreaterEqual(distance, UNRELATED_MIN_METERS)
        self.assertIn("300米", basis)

        def lookup(queries: list[dict[str, str]]) -> list[dict]:
            return [
                _search_response(
                    query=queries[0]["query"],
                    name="合同门店",
                    address="甲路1号",
                    location="113.95,22.99",
                    poi_id="contract",
                ),
                _search_response(
                    query=queries[1]["query"],
                    name="照片地点",
                    address="乙路8号",
                    location="113.96,23.0",
                    poi_id="watermark",
                ),
            ]

        store_match, _, resolution = _determine_store_match(
            "合同门店",
            None,
            "照片地点",
            StoreLocationResolver(mcp_lookup=lookup, registry={"entries": []}),
        )
        self.assertEqual(store_match, "location_unverified")
        self.assertEqual(resolution["status"], "unrelated")
        self.assertEqual(resolution["confidence"], "low")

    def test_same_building_address_never_becomes_distant_location_error(self) -> None:
        relationship, distance, basis = classify_poi_relationship(
            {
                "name": "合同门店",
                "address": "东莞市横沥镇中山中路88号南铭购物乐园二楼",
                "poi_id": "contract",
                "longitude": 113.95,
                "latitude": 22.99,
            },
            {
                "name": "南铭购物乐园",
                "address": "东莞市横沥镇中山中路88号",
                "poi_id": "watermark",
                "longitude": 114.0,
                "latitude": 23.04,
            },
        )
        self.assertEqual(relationship, "ambiguous")
        self.assertIsNotNone(distance)
        self.assertGreaterEqual(distance, UNRELATED_MIN_METERS)
        self.assertIn("数据互相冲突", basis)

    def test_no_mcp_and_no_registry_is_pending_not_mismatch(self) -> None:
        resolver = StoreLocationResolver(mcp_lookup=None, registry={"entries": []})
        store_match, basis, resolution = _determine_store_match(
            "合同门店",
            None,
            "另一个地点",
            resolver,
        )
        self.assertEqual(store_match, "location_unverified")
        self.assertEqual(resolution["status"], "unavailable")
        self.assertEqual(resolution["confidence"], "low")
        self.assertEqual(resolution["mcp_status"], "not_configured")
        self.assertIn("地点置信度低", basis)

    def test_mcp_failure_does_not_leak_secret_or_turn_into_error(self) -> None:
        def lookup(_queries: list[dict[str, str]]) -> list[dict]:
            raise RuntimeError("https://mcp.map.baidu.com/mcp?ak=DO_NOT_LEAK")

        resolver = StoreLocationResolver(mcp_lookup=lookup, registry={"entries": []})
        resolution = resolver.resolve(
            contract_name="合同门店",
            contract_address=None,
            visible_location="另一个地点",
            city_hint="东莞市",
        )
        self.assertEqual(resolution["status"], "unavailable")
        self.assertEqual(resolution["mcp_status"], "unavailable")
        self.assertNotIn("DO_NOT_LEAK", str(resolution))

    def test_single_baidu_candidate_can_be_selected_after_name_translation(self) -> None:
        candidate = {
            "name": "Nanming Shopping Paradise",
            "address": "",
            "poi_id": "mall-1",
            "longitude": 113.970968,
            "latitude": 23.0149396,
        }
        selected, basis = select_unique_poi("东莞市·南铭购物乐园", [candidate])
        self.assertEqual(selected, candidate)
        self.assertIn("一个地点候选", basis)

    def test_multiple_translated_baidu_candidates_remain_ambiguous(self) -> None:
        candidates = [
            {
                "name": "Tingmuzhi Lifestyle Supermarket",
                "address": "",
                "poi_id": f"store-{index}",
                "longitude": 113.9 + index / 100,
                "latitude": 23.0,
            }
            for index in range(3)
        ]
        selected, basis = select_unique_poi("挺拇指生活超市（横沥店）", candidates)
        self.assertIsNone(selected)
        self.assertIn("没有找到", basis)

    def test_baidu_mcp_is_disabled_without_baidu_api_key(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch(
            "audit_core.location_resolution._windows_user_environment",
            return_value="",
        ):
            self.assertIsNone(baidu_maps_mcp_lookup_from_environment())

    def test_baidu_mcp_reads_windows_user_configuration(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch(
            "audit_core.location_resolution._windows_user_environment",
            return_value="configured-test-ak",
        ):
            self.assertIsNotNone(baidu_maps_mcp_lookup_from_environment())

    def test_baidu_mcp_url_uses_fixed_endpoint_and_url_encodes_api_key(self) -> None:
        endpoint = _baidu_maps_mcp_url("fake+A&B key")
        self.assertEqual(
            endpoint,
            f"{BAIDU_MAPS_MCP_ENDPOINT}?ak=fake%2BA%26B+key",
        )

    def test_city_hint_ignores_market_and_extracts_city_after_province(self) -> None:
        self.assertIsNone(city_hint_from_values("龙眼市场（龙眼路）"))
        self.assertEqual(city_hint_from_values("广东省东莞市横沥镇"), "东莞市")


if __name__ == "__main__":
    unittest.main()
