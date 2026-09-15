# Matchline 逐源抓取可行性审计（2026-09-15）

> 本报告由 `league_platform.source_research` 从 registry 与 `SourceId` 并集生成。报告生成器不联网；所有无证据项均保留为明确的未尝试、未配置、权利未验证或隔离状态。

- Schema：`matchline.source_research.v1`
- 生成时间：`2026-09-16T03:43:00+08:00`（由调用方显式传入）
- 快照 `as_of`：`2026-09-15T19:03:30.341536+00:00`
- 快照 SHA-256：`b064d56c32cd588ccf134bb155876b418d52bb6407d0502250ff5072defacf89`
- 覆盖来源数：`47`
- 合规边界：不绕过 robots、WAF、验证码、登录、TLS、速率限制或授权；受限来源不进入模型/正式发布。

## 判定字段

每行都包含权利依据、terms/license URL、robots/access、allowlist/redirect/size/timeout、限速、解析、字段、实体联结、双时钟、SHA-256、实际 HTTP/运行状态、替代方案和 display/model/publication eligibility。`not_recorded_by_adapter` 表示适配器没有导出该 HTTP header，不表示响应成功。

| 来源 | 权利结论 | HTTP/运行状态 | 网络 | 记录数 | display | model | publication |
|---|---|---|---:|---:|---|---|---|
| bundesliga_public_pages | first_party_access_reuse_terms_not_verified | unavailable | not opened | — | blocked_pending_terms_review | blocked | blocked |
| cfl_official_current | rights_unverified | rights_blocked | not opened | — | blocked_pending_rights_review | blocked | blocked |
| checkbestodds_public_pages | rights_unverified | not_observed | not opened | — | blocked_pending_rights_review | blocked | blocked |
| clubelo_public_ratings | rights_unverified | quarantined | not opened | — | blocked_pending_rights_review | blocked | blocked |
| crawl4ai_allowlisted_pages | execution_layer_not_fact_source | unavailable | not opened | — | conditional_per_declared_page | blocked_by_default | blocked_by_default |
| espn_injury_reports | provider_permission_required | rights_blocked | not opened | — | blocked | blocked | blocked |
| espn_market_summary | provider_permission_required | rights_blocked | not opened | — | blocked | blocked | blocked |
| espn_schedule_summary | provider_permission_required | rights_blocked | not opened | — | blocked | blocked | blocked |
| espn_team_rosters | provider_permission_required | rights_blocked | not opened | — | blocked | blocked | blocked |
| fbref_public_stats | rights_unverified | quarantined | not opened | — | blocked_pending_rights_review | blocked | blocked |
| five_hundred_league_public_pages | rights_unverified | not_observed | not opened | — | blocked_pending_rights_review | blocked | blocked |
| football_data_china_public_csv | rights_unverified | not_observed | not opened | — | blocked_pending_rights_review | blocked | blocked |
| football_data_historical | public_download_rights_not_stated_for_product_use | training_only | not opened | — | blocked_current | blocked_current | blocked_current |
| fotmob_public_api | robots_policy_blocked | blocked_by_robots | not opened | — | blocked | blocked | blocked |
| laliga_official_match_directory | rights_unverified | unavailable | not opened | — | blocked_pending_rights_review | blocked | blocked |
| laliga_official_news | first_party_access_reuse_terms_not_verified | unavailable | not opened | — | blocked_pending_terms_review | blocked | blocked |
| laliga_public_pages | first_party_access_reuse_terms_not_verified | unavailable | not opened | — | blocked_pending_terms_review | blocked | blocked |
| lazq_public_mirror | rights_unverified | quarantined | not opened | — | blocked_pending_rights_review | blocked | blocked |
| legacy_anti_waf_relay | forbidden_access_control_bypass_artifact | forbidden | not opened | — | blocked | blocked | blocked |
| ligue1_official_news | first_party_access_reuse_terms_not_verified | unavailable | not opened | — | blocked_pending_terms_review | blocked | blocked |
| met_norway_weather | verified_cc_by_forecast_with_identifying_user_agent | unavailable | opened | — | allowed_with_attribution | conditional_exact_coordinates_and_time | conditional_with_attribution |
| oddstorm_market_comparison | provider_permission_required | rights_blocked | not opened | — | blocked | blocked | blocked |
| oddstorm_market_history | provider_permission_required | rights_blocked | not opened | — | blocked | blocked | blocked |
| official_bundesliga_lineups | first_party_access_reuse_terms_not_verified | rights_blocked | not opened | — | blocked_pending_terms_review | blocked | blocked |
| official_laliga_lineups | first_party_access_reuse_terms_not_verified | rights_blocked | not opened | — | blocked_pending_terms_review | blocked | blocked |
| official_league_lineups | first_party_access_reuse_terms_not_verified | unavailable | not opened | — | blocked_pending_terms_review | blocked | blocked |
| official_ligue1_lineups | first_party_access_reuse_terms_not_verified | rights_blocked | not opened | — | blocked_pending_terms_review | blocked | blocked |
| official_premier_league_lineups | first_party_access_reuse_terms_not_verified | rights_blocked | not opened | — | blocked_pending_terms_review | blocked | blocked |
| official_serie_a_lineups | first_party_access_reuse_terms_not_verified | rights_blocked | not opened | — | blocked_pending_terms_review | blocked | blocked |
| open_meteo | rights_unverified | rights_blocked | not opened | — | blocked_pending_rights_review | blocked | blocked |
| open_meteo_geocoding | free_tier_noncommercial_cc_by_only | rights_blocked | not opened | — | blocked_for_current_commercial_release | blocked_pending_commercial_plan | blocked_pending_commercial_plan |
| open_meteo_weather | free_tier_noncommercial_cc_by_only | rights_blocked | not opened | — | blocked_for_current_commercial_release | blocked_pending_commercial_plan | blocked_pending_commercial_plan |
| openfootball_current | verified_cc0_public_domain | fresh | opened | — | conditional_after_admission | conditional_after_causal_gate | conditional_after_maturity_gate |
| openfootball_historical | verified_cc0_public_domain | training_only | not opened | — | conditional_after_admission | conditional_after_causal_gate | conditional_after_maturity_gate |
| openligadb_secondary_results | verified_odbl_isolated_display_current | fresh | opened | — | allowed_isolated | blocked | blocked |
| osm_geodata | rights_metadata_present_adapter_contract_pending | future_disabled | not opened | — | blocked_until_adapter_contract | blocked | blocked |
| pappalardo_wyscout_historical | rights_metadata_present_adapter_contract_pending | future_disabled | not opened | — | blocked_until_adapter_contract | blocked | blocked |
| premier_league_public_pages | first_party_access_reuse_terms_not_verified | unavailable | not opened | — | blocked_pending_terms_review | blocked | blocked |
| public_rss_news | rights_unverified | rights_blocked | not opened | — | blocked_pending_rights_review | blocked | blocked |
| seriea_public_pages | first_party_access_reuse_terms_not_verified | unavailable | not opened | — | blocked_pending_terms_review | blocked | blocked |
| sevenm_csl_public_fixture_script | rights_unverified | not_observed | not opened | — | blocked_pending_rights_review | blocked | blocked |
| sofascore_prematch | rights_unverified | rights_blocked | not opened | — | blocked_pending_rights_review | blocked | blocked |
| sofifa_public_reference | robots_policy_blocked | blocked_by_robots | not opened | — | blocked | blocked | blocked |
| sports_lottery_official | rights_unverified | rights_blocked | not opened | — | blocked_pending_rights_review | blocked | blocked |
| understat_xg | rights_unverified | rights_blocked | not opened | — | blocked_pending_rights_review | blocked | blocked |
| whoscored_public_pages | rights_unverified | unavailable | not opened | — | blocked_pending_rights_review | blocked | blocked |
| wikidata_entities | verified_cc0_structured_data | fresh | opened | 1 | allowed_with_attribution_preference | conditional_exact_venue_chain | conditional_with_provenance |

## 逐源详细记录

### `bundesliga_public_pages` — 德甲官方公开比赛页

- Adapter：`league_platform.live_sources.crawl4ai`；主机：`bundesliga.com`
- 权利：first_party_access_reuse_terms_not_verified；The first-party page/API is publicly discoverable, but public visibility does not grant automated extraction or commercial redistribution rights.
- 条款/许可证：https://www.bundesliga.com/en/bundesliga/info/legal-notices, https://www.bundesliga.com/robots.txt
- Robots/access：robots/legal notice reserves text-and-data-mining rights; no automated fetch；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["bundesliga.com"], "max_response_bytes": 2097152, "path_policy": "explicit_path_robots_allowlist_required", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 10, "normal_minutes": 60, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.crawl4ai`；声明字段：`provider_native_id, teams_or_entities, observed_fields, observed_at`；已观测：``；缺失：`provider_native_id, teams_or_entities, observed_fields, observed_at`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.bundesliga_public_pages"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_observed", "status": "unavailable", "status_codes": []}`
- 状态/失败：`{"reason": "runtime payload or diagnostics are present; see errors and coverage", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "unavailable"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_pending_terms_review`，model=`blocked`，publication=`blocked`

### `cfl_official_current` — 中足联官方中超当前赛程与赛果

- Adapter：`league_platform.live_sources.cfl_official`；主机：`api.cfl-china.cn`
- 权利：rights_unverified；No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["api.cfl-china.cn"], "max_response_bytes": 2097152, "path_policy": "official_public_frontend_api_https_no_redirect_bounded_reads", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 10, "normal_minutes": 30, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.cfl_official`；声明字段：`provider_native_id, teams_or_entities, observed_fields, observed_at`；已观测：``；缺失：`provider_native_id, teams_or_entities, observed_fields, observed_at`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.cfl_official"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "cfl_official", "state": "rights_blocked"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_pending_rights_review`，model=`blocked`，publication=`blocked`

### `checkbestodds_public_pages` — checkbestodds_public_pages

- Adapter：`league_platform.sources.checkbestodds`；主机：`checkbestodds.com`
- 权利：rights_unverified；No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["checkbestodds.com"], "max_response_bytes": 2097152, "path_policy": "policy inventory requires explicit adapter contract", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": null, "normal_minutes": null, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.sources.checkbestodds`；声明字段：`native_match_id, market, line, price, observed_at, settlement_rule`；已观测：``；缺失：`native_match_id, market, line, price, observed_at, settlement_rule`
- 实体联结：exact competition + UTC kickoff + canonical home/away identity; low-confidence rows remain display-only
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_types": [], "evidence": ["no runtime section for this policy-only source"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "state": "not_observed", "status": "not_observed_for_this_snapshot", "status_codes": []}`
- 状态/失败：`{"reason": "no source section was present in this runtime snapshot", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "not_observed"}`
- 替代方案：Licensed market feed with explicit redistribution/settlement rights; otherwise market baseline stays unavailable
- Eligibility：display=`blocked_pending_rights_review`，model=`blocked`，publication=`blocked`

### `clubelo_public_ratings` — ClubElo 公共评级

- Adapter：`league_platform.live_sources.crawl4ai`；主机：`www.clubelo.com`
- 权利：rights_unverified；No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["www.clubelo.com"], "max_response_bytes": 2097152, "path_policy": "public_https_bounded_probe_only", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 60, "normal_minutes": 360, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.crawl4ai`；声明字段：`team_or_match_id, observed_at, xg_or_rating, source_payload_hash`；已观测：``；缺失：`team_or_match_id, observed_at, xg_or_rating, source_payload_hash`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.clubelo_public_ratings"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "quarantined", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "quarantined"}`
- 替代方案：Use OpenFootball historical/current facts or obtain provider permission; do not bypass robots/WAF/TLS policy
- Eligibility：display=`blocked_pending_rights_review`，model=`blocked`，publication=`blocked`

### `crawl4ai_allowlisted_pages` — Crawl4AI 抓取引擎（白名单网页运行时）

- Adapter：`league_platform.live_sources.crawl4ai`；主机：`无（执行层/未来适配器）`
- 权利：execution_layer_not_fact_source；Crawl4AI is an execution layer; each page needs an independently verified source identity, rights and provenance.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": [], "max_response_bytes": 2097152, "path_policy": "explicit_allowlist_robots_fail_closed_same_host_redirect", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 10, "normal_minutes": 60, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.crawl4ai`；声明字段：`declared_source_id, final_url, parser_contract, raw_hash, observed_at`；已观测：``；缺失：`declared_source_id, final_url, parser_contract, raw_hash, observed_at`
- 实体联结：declared source_id is mandatory; final URL must remain same-host allowlist; no aggregate page-to-source relabeling
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 1, "errors": [{"enters_model": false, "error": "Crawl4AI evidence archive path is volatile", "model_eligible": false, "network_opened": false, "stage": "archive_policy"}], "evidence": ["snapshot.runtime.crawl4ai"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "failed", "status": "unavailable", "status_codes": []}`
- 状态/失败：`{"reason": "runtime payload or diagnostics are present; see errors and coverage", "runtime_error_count": 1, "runtime_errors": [{"enters_model": false, "error": "Crawl4AI evidence archive path is volatile", "model_eligible": false, "network_opened": false, "stage": "archive_policy"}], "runtime_key": "crawl4ai", "state": "unavailable"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`conditional_per_declared_page`，model=`blocked_by_default`，publication=`blocked_by_default`

### `espn_injury_reports` — ESPN 公开联盟伤停报告

- Adapter：`league_platform.live_sources.espn_injuries`；主机：`site.api.espn.com, site.web.api.espn.com`
- 权利：provider_permission_required；Disney/ESPN terms prohibit automated extraction and data collection without an express written permission path.
- 条款/许可证：https://disneytermsofuse.com/english/
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["site.api.espn.com", "site.web.api.espn.com"], "max_response_bytes": 2097152, "path_policy": "provider_terms_block_unlicensed_automated_access_and_commercial_use", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 0, "normal_minutes": 0, "provider_rule": "polling disabled before network by rights/robots gate"}`
- Parser：`league_platform.live_sources.espn_injuries`；声明字段：`native_event_or_team_id, team, player, status, score_or_market_fields, observed_at`；已观测：``；缺失：`native_event_or_team_id, team, player, status, score_or_market_fields, observed_at`
- 实体联结：provider-native event/team identity only; no name-only or cross-provider fuzzy join
- 时间：observed_at must precede kickoff/freeze cutoff for model use; publication/effective time is not inferred from request time
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.espn_injuries"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "espn_injuries", "state": "rights_blocked"}`
- 替代方案：Licensed sports-data provider or first-party permission; OpenFootball remains fixture/result authority
- Eligibility：display=`blocked`，model=`blocked`，publication=`blocked`

### `espn_market_summary` — ESPN 事件摘要市场与名单

- Adapter：`league_platform.live_sources.espn_market`；主机：`site.api.espn.com`
- 权利：provider_permission_required；Disney/ESPN terms prohibit automated extraction and data collection without an express written permission path.
- 条款/许可证：https://disneytermsofuse.com/english/
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["site.api.espn.com"], "max_response_bytes": 2097152, "path_policy": "provider_terms_block_unlicensed_automated_access_and_commercial_use", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 0, "normal_minutes": 0, "provider_rule": "polling disabled before network by rights/robots gate"}`
- Parser：`league_platform.live_sources.espn_market`；声明字段：`native_event_or_team_id, team, player, status, score_or_market_fields, observed_at`；已观测：``；缺失：`native_event_or_team_id, team, player, status, score_or_market_fields, observed_at`
- 实体联结：provider-native event/team identity only; no name-only or cross-provider fuzzy join
- 时间：observed_at must precede kickoff/freeze cutoff for model use; publication/effective time is not inferred from request time
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.espn_markets"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "espn_markets", "state": "rights_blocked"}`
- 替代方案：Licensed sports-data provider or first-party permission; OpenFootball remains fixture/result authority
- Eligibility：display=`blocked`，model=`blocked`，publication=`blocked`

### `espn_schedule_summary` — ESPN 赛程、赛果与事件摘要

- Adapter：`league_platform.live_sources.espn`；主机：`site.api.espn.com, site.web.api.espn.com`
- 权利：provider_permission_required；Disney/ESPN terms prohibit automated extraction and data collection without an express written permission path.
- 条款/许可证：https://disneytermsofuse.com/english/
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["site.api.espn.com", "site.web.api.espn.com"], "max_response_bytes": 2097152, "path_policy": "provider_terms_block_unlicensed_automated_access_and_commercial_use", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 0, "normal_minutes": 0, "provider_rule": "polling disabled before network by rights/robots gate"}`
- Parser：`league_platform.live_sources.espn`；声明字段：`native_event_or_team_id, team, player, status, score_or_market_fields, observed_at`；已观测：``；缺失：`native_event_or_team_id, team, player, status, score_or_market_fields, observed_at`
- 实体联结：provider-native event/team identity only; no name-only or cross-provider fuzzy join
- 时间：observed_at must precede kickoff/freeze cutoff for model use; publication/effective time is not inferred from request time
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.espn"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "espn", "state": "rights_blocked"}`
- 替代方案：Licensed sports-data provider or first-party permission; OpenFootball remains fixture/result authority
- Eligibility：display=`blocked`，model=`blocked`，publication=`blocked`

### `espn_team_rosters` — ESPN 公开球队名单

- Adapter：`league_platform.live_sources.espn_roster`；主机：`site.web.api.espn.com`
- 权利：provider_permission_required；Disney/ESPN terms prohibit automated extraction and data collection without an express written permission path.
- 条款/许可证：https://disneytermsofuse.com/english/
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["site.web.api.espn.com"], "max_response_bytes": 2097152, "path_policy": "provider_terms_block_unlicensed_automated_access_and_commercial_use", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 0, "normal_minutes": 0, "provider_rule": "polling disabled before network by rights/robots gate"}`
- Parser：`league_platform.live_sources.espn_roster`；声明字段：`native_event_or_team_id, team, player, status, score_or_market_fields, observed_at`；已观测：``；缺失：`native_event_or_team_id, team, player, status, score_or_market_fields, observed_at`
- 实体联结：provider-native event/team identity only; no name-only or cross-provider fuzzy join
- 时间：observed_at must precede kickoff/freeze cutoff for model use; publication/effective time is not inferred from request time
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.espn_rosters"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "espn_rosters", "state": "rights_blocked"}`
- 替代方案：Licensed sports-data provider or first-party permission; OpenFootball remains fixture/result authority
- Eligibility：display=`blocked`，model=`blocked`，publication=`blocked`

### `fbref_public_stats` — FBref 公共球队与球员统计

- Adapter：`league_platform.live_sources.crawl4ai`；主机：`fbref.com`
- 权利：rights_unverified；No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.
- 条款/许可证：https://www.sports-reference.com/data_use.html
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["fbref.com"], "max_response_bytes": 2097152, "path_policy": "public_https_current_probe_403", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 0, "normal_minutes": 0, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.crawl4ai`；声明字段：`team_or_match_id, observed_at, xg_or_rating, source_payload_hash`；已观测：``；缺失：`team_or_match_id, observed_at, xg_or_rating, source_payload_hash`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.fbref_public_stats"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "quarantined", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "quarantined"}`
- 替代方案：Use OpenFootball historical/current facts or obtain provider permission; do not bypass robots/WAF/TLS policy
- Eligibility：display=`blocked_pending_rights_review`，model=`blocked`，publication=`blocked`

### `five_hundred_league_public_pages` — five_hundred_league_public_pages

- Adapter：`league_platform.live_sources.five_hundred_league`；主机：`500league.com`
- 权利：rights_unverified；No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["500league.com"], "max_response_bytes": 2097152, "path_policy": "policy inventory requires explicit adapter contract", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": null, "normal_minutes": null, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.five_hundred_league`；声明字段：`fixture_or_match_id, teams, kickoff_or_date, score, historical_market_fields`；已观测：``；缺失：`fixture_or_match_id, teams, kickoff_or_date, score, historical_market_fields`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_types": [], "evidence": ["no runtime section for this policy-only source"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "state": "not_observed", "status": "not_observed_for_this_snapshot", "status_codes": []}`
- 状态/失败：`{"reason": "no source section was present in this runtime snapshot", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "not_observed"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_pending_rights_review`，model=`blocked`，publication=`blocked`

### `football_data_china_public_csv` — football_data_china_public_csv

- Adapter：`league_platform.sources.football_data_china`；主机：`football-data.cn`
- 权利：rights_unverified；No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["football-data.cn"], "max_response_bytes": 2097152, "path_policy": "policy inventory requires explicit adapter contract", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": null, "normal_minutes": null, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.sources.football_data_china`；声明字段：`fixture_or_match_id, teams, kickoff_or_date, score, historical_market_fields`；已观测：``；缺失：`fixture_or_match_id, teams, kickoff_or_date, score, historical_market_fields`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_types": [], "evidence": ["no runtime section for this policy-only source"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "state": "not_observed", "status": "not_observed_for_this_snapshot", "status_codes": []}`
- 状态/失败：`{"reason": "no source section was present in this runtime snapshot", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "not_observed"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_pending_rights_review`，model=`blocked`，publication=`blocked`

### `football_data_historical` — Football-Data.co.uk 历史赛果与赔率 CSV

- Adapter：`league_platform.data_manifest`；主机：`www.football-data.co.uk`
- 权利：public_download_rights_not_stated_for_product_use；The official site offers downloadable quantitative-testing files, but this inventory has no explicit commercial redistribution grant; keep training use isolated and re-verify before product use.
- 条款/许可证：https://www.football-data.co.uk/data.php, https://www.football-data.co.uk/downloadm.php
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["www.football-data.co.uk"], "max_response_bytes": 2097152, "path_policy": "public_https_csv_training_only", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 0, "normal_minutes": 1440, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.data_manifest`；声明字段：`fixture_or_match_id, teams, kickoff_or_date, score, historical_market_fields`；已观测：``；缺失：`fixture_or_match_id, teams, kickoff_or_date, score, historical_market_fields`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：historical source timestamp/season boundary is retained; rows cannot become current or future observations
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.football_data_historical"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "training_only", "status": "training_only", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "training_only"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_current`，model=`blocked_current`，publication=`blocked_current`

### `fotmob_public_api` — FotMob 公共 API

- Adapter：`league_platform.live_sources.fotmob`；主机：`www.fotmob.com`
- 权利：robots_policy_blocked；The current policy records a robots/API restriction; no network request is opened until that policy changes.
- 条款/许可证：https://www.fotmob.com/robots.txt
- Robots/access：robots policy blocked; no request is opened；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["www.fotmob.com"], "max_response_bytes": 2097152, "path_policy": "robots_disallowed", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 0, "normal_minutes": 0, "provider_rule": "polling disabled before network by rights/robots gate"}`
- Parser：`league_platform.live_sources.fotmob`；声明字段：`provider_native_id, teams_or_entities, observed_fields, observed_at`；已观测：``；缺失：`provider_native_id, teams_or_entities, observed_fields, observed_at`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.fotmob"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "blocked_by_robots", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "fotmob", "state": "blocked_by_robots"}`
- 替代方案：Use OpenFootball historical/current facts or obtain provider permission; do not bypass robots/WAF/TLS policy
- Eligibility：display=`blocked`，model=`blocked`，publication=`blocked`

### `laliga_official_match_directory` — 西甲官方公开比赛目录

- Adapter：`league_platform.live_sources.laliga`；主机：`apim.laliga.com`
- 权利：rights_unverified；No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["apim.laliga.com"], "max_response_bytes": 2097152, "path_policy": "public_https_bounded_no_redirect", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 2, "normal_minutes": 30, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.laliga`；声明字段：`provider_native_id, teams_or_entities, observed_fields, observed_at`；已观测：``；缺失：`provider_native_id, teams_or_entities, observed_fields, observed_at`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 1, "errors": [{"error": "match_directory_diagnostic_missing"}], "evidence": ["snapshot.runtime.laliga_official"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "failed", "status": "unavailable", "status_codes": []}`
- 状态/失败：`{"reason": "runtime payload or diagnostics are present; see errors and coverage", "runtime_error_count": 1, "runtime_errors": [{"error": "match_directory_diagnostic_missing"}], "runtime_key": "laliga_official", "state": "unavailable"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_pending_rights_review`，model=`blocked`，publication=`blocked`

### `laliga_official_news` — 西甲官方公开新闻页

- Adapter：`league_platform.live_sources.crawl4ai`；主机：`www.laliga.com`
- 权利：first_party_access_reuse_terms_not_verified；The first-party page/API is publicly discoverable, but public visibility does not grant automated extraction or commercial redistribution rights.
- 条款/许可证：https://www.laliga.com/en-GB/legal/legal-web
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["www.laliga.com"], "max_response_bytes": 2097152, "path_policy": "explicit_path_robots_allowlist_required", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 10, "normal_minutes": 60, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.crawl4ai`；声明字段：`article_url, title, published_at, team_or_fixture_reference, content_hash`；已观测：``；缺失：`article_url, title, published_at, team_or_fixture_reference, content_hash`
- 实体联结：explicit fixture/team reference and article timestamp; otherwise global source-health evidence only
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.laliga_official_news"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_observed", "status": "unavailable", "status_codes": []}`
- 状态/失败：`{"reason": "runtime payload or diagnostics are present; see errors and coverage", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "unavailable"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_pending_terms_review`，model=`blocked`，publication=`blocked`

### `laliga_public_pages` — 西甲官方公开比赛页

- Adapter：`league_platform.live_sources.crawl4ai`；主机：`www.laliga.com`
- 权利：first_party_access_reuse_terms_not_verified；The first-party page/API is publicly discoverable, but public visibility does not grant automated extraction or commercial redistribution rights.
- 条款/许可证：https://www.laliga.com/en-GB/legal/legal-web
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["www.laliga.com"], "max_response_bytes": 2097152, "path_policy": "explicit_path_robots_allowlist_required", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 10, "normal_minutes": 60, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.crawl4ai`；声明字段：`provider_native_id, teams_or_entities, observed_fields, observed_at`；已观测：``；缺失：`provider_native_id, teams_or_entities, observed_fields, observed_at`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.laliga_public_pages"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_observed", "status": "unavailable", "status_codes": []}`
- 状态/失败：`{"reason": "runtime payload or diagnostics are present; see errors and coverage", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "unavailable"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_pending_terms_review`，model=`blocked`，publication=`blocked`

### `lazq_public_mirror` — Lazq 公开镜像赔率

- Adapter：`league_platform.live_sources.lazq`；主机：`api.lazq.com`
- 权利：rights_unverified；No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["api.lazq.com"], "max_response_bytes": 2097152, "path_policy": "public_https_bounded_requests", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 10, "normal_minutes": 60, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.lazq`；声明字段：`native_match_id, market, line, price, observed_at, settlement_rule`；已观测：``；缺失：`native_match_id, market, line, price, observed_at, settlement_rule`
- 实体联结：exact competition + UTC kickoff + canonical home/away identity; low-confidence rows remain display-only
- 时间：quote observed_at and settlement line are mandatory; stale/history rows cannot become current odds
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.lazq_public_mirror"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "quarantined", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "quarantined"}`
- 替代方案：Licensed market feed with explicit redistribution/settlement rights; otherwise market baseline stays unavailable
- Eligibility：display=`blocked_pending_rights_review`，model=`blocked`，publication=`blocked`

### `legacy_anti_waf_relay` — 旧项目 relay_sporttery.sh / 4090 中转

- Adapter：`external-archive:cleanup-v276-2026-08-26/legacy-entrypoints/wc_analysis/relay_sporttery.sh`；主机：`无（执行层/未来适配器）`
- 权利：forbidden_access_control_bypass_artifact；Legacy relay logic would bypass access controls and is represented only as a negative audit record.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": [], "max_response_bytes": 2097152, "path_policy": "would_bypass_access_controls", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 0, "normal_minutes": 0, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`external-archive:cleanup-v276-2026-08-26/legacy-entrypoints/wc_analysis/relay_sporttery.sh`；声明字段：`provider_native_id, teams_or_entities, observed_fields, observed_at`；已观测：``；缺失：`provider_native_id, teams_or_entities, observed_fields, observed_at`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.legacy_anti_waf_relay"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "forbidden", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "forbidden"}`
- 替代方案：Remove dependency and use an explicitly licensed/allowlisted provider
- Eligibility：display=`blocked`，model=`blocked`，publication=`blocked`

### `ligue1_official_news` — 法甲官方公开新闻页

- Adapter：`league_platform.live_sources.crawl4ai`；主机：`ligue1.com`
- 权利：first_party_access_reuse_terms_not_verified；The first-party page/API is publicly discoverable, but public visibility does not grant automated extraction or commercial redistribution rights.
- 条款/许可证：https://ligue1.com/en/legal/cgu
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["ligue1.com"], "max_response_bytes": 2097152, "path_policy": "explicit_path_robots_allowlist_required", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 10, "normal_minutes": 60, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.crawl4ai`；声明字段：`article_url, title, published_at, team_or_fixture_reference, content_hash`；已观测：``；缺失：`article_url, title, published_at, team_or_fixture_reference, content_hash`
- 实体联结：explicit fixture/team reference and article timestamp; otherwise global source-health evidence only
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.ligue1_official_news"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_observed", "status": "unavailable", "status_codes": []}`
- 状态/失败：`{"reason": "runtime payload or diagnostics are present; see errors and coverage", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "unavailable"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_pending_terms_review`，model=`blocked`，publication=`blocked`

### `met_norway_weather` — MET Norway Locationforecast 天气

- Adapter：`league_platform.live_sources.met_norway`；主机：`api.met.no`
- 权利：verified_cc_by_forecast_with_identifying_user_agent；MET Norway publishes CC-BY weather with identifying User-Agent, attribution, cache and traffic obligations; forecast is not historical training data.
- 条款/许可证：https://api.met.no/doc/TermsOfService, https://creativecommons.org/licenses/by/4.0/
- Robots/access：identified User-Agent, HTTPS, max four-decimal coordinates, cache/Expires, stop on 429；网络判定 `attempted_under_declared_policy`
- Allowlist/限制：`{"concurrency": "bounded; comply with 20 requests/second application ceiling", "coordinates": "truncate to four decimals", "host_allowlist": ["api.met.no"], "max_response_bytes": 5242880, "path_policy": "/weatherapi/locationforecast/2.0/compact only", "redirects": "reject through allowlist validation", "timeout_seconds": 30}`
- 限速：`{"near_kickoff_minutes": 30, "normal_minutes": 180, "provider_rule": "<=20 req/s/application; honor Expires/Last-Modified; stop on 429"}`
- Parser：`league_platform.live_sources.met_norway`；声明字段：`forecast_at, temperature_c, humidity_percent, wind_speed_mps, precipitation_mm, symbol_code`；已观测：``；缺失：`forecast_at, temperature_c, humidity_percent, wind_speed_mps, precipitation_mm, symbol_code`
- 实体联结：requires already verified venue coordinates; no team-name or home-ground guessing
- 时间：forecast_at is the target hour; retrieved_at is observation time; forecast must be within provider horizon and before freeze
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": true, "content_types": ["application/json body parsed by adapter; HTTP header not exported"], "error_count": 1, "errors": [{"fixture_id": "openfootball:eredivisie:a9d16d2d12664c4597d5dab5", "reason": "MET Norway response has no exact forecast hour 2026-09-20T10:00:00+00:00"}], "evidence": ["https://api.met.no/weatherapi/locationforecast/2.0/compact"], "network_opened": true, "parser_boundary": "response reached JSON parser but exact target hour was absent; no weather row was admitted", "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "request_started_at": "2026-09-15T18:37:12.259037+00:00", "response_observed_at": "2026-09-15T18:37:12.259037+00:00", "source_id": "met_norway_weather", "state": "failed", "status": "unavailable", "status_codes": []}`
- 状态/失败：`{"reason": "dated operator probe reached the source but parser/contract admission failed", "runtime_error_count": 1, "runtime_errors": [{"fixture_id": "openfootball:eredivisie:a9d16d2d12664c4597d5dab5", "reason": "MET Norway response has no exact forecast hour 2026-09-20T10:00:00+00:00"}], "runtime_key": "met_norway_weather", "state": "unavailable"}`
- 替代方案：Use the other permitted CC0/CC-BY lane only for its own fields; keep missing venue/weather explicit
- Eligibility：display=`allowed_with_attribution`，model=`conditional_exact_coordinates_and_time`，publication=`conditional_with_attribution`

### `oddstorm_market_comparison` — OddStorm 市场对照（授权阻断）

- Adapter：`league_platform.live_sources.oddstorm`；主机：`www.oddstorm.com`
- 权利：provider_permission_required；OddStorm terms prohibit automated scraping, mirroring, redistribution and resale absent express written permission.
- 条款/许可证：https://www.oddstorm.com/terms
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["www.oddstorm.com"], "max_response_bytes": 2097152, "path_policy": "provider_terms_forbid_automated_scraping_mirroring_redistribution_and_resale", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 0, "normal_minutes": 0, "provider_rule": "polling disabled before network by rights/robots gate"}`
- Parser：`league_platform.live_sources.oddstorm`；声明字段：`native_match_id, market, line, price, observed_at, settlement_rule`；已观测：``；缺失：`native_match_id, market, line, price, observed_at, settlement_rule`
- 实体联结：exact competition + UTC kickoff + canonical home/away identity; low-confidence rows remain display-only
- 时间：quote observed_at and settlement line are mandatory; stale/history rows cannot become current odds
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.oddstorm"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "oddstorm", "state": "rights_blocked"}`
- 替代方案：Licensed market feed with explicit redistribution/settlement rights; otherwise market baseline stays unavailable
- Eligibility：display=`blocked`，model=`blocked`，publication=`blocked`

### `oddstorm_market_history` — oddstorm_market_history

- Adapter：`league_platform.live_sources.oddstorm`；主机：`www.oddstorm.com`
- 权利：provider_permission_required；OddStorm terms prohibit automated scraping, mirroring, redistribution and resale absent express written permission.
- 条款/许可证：https://www.oddstorm.com/terms
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["www.oddstorm.com"], "max_response_bytes": 2097152, "path_policy": "policy inventory requires explicit adapter contract", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 0, "normal_minutes": 0, "provider_rule": "polling disabled before network by rights/robots gate"}`
- Parser：`league_platform.live_sources.oddstorm`；声明字段：`native_match_id, market, line, price, observed_at, settlement_rule`；已观测：``；缺失：`native_match_id, market, line, price, observed_at, settlement_rule`
- 实体联结：exact competition + UTC kickoff + canonical home/away identity; low-confidence rows remain display-only
- 时间：quote observed_at and settlement line are mandatory; stale/history rows cannot become current odds
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.oddstorm"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": null, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "oddstorm", "state": "rights_blocked"}`
- 替代方案：Licensed market feed with explicit redistribution/settlement rights; otherwise market baseline stays unavailable
- Eligibility：display=`blocked`，model=`blocked`，publication=`blocked`

### `official_bundesliga_lineups` — official_bundesliga_lineups

- Adapter：`league_platform.live_sources.bundesliga`；主机：`bundesliga.com`
- 权利：first_party_access_reuse_terms_not_verified；The first-party page/API is publicly discoverable, but public visibility does not grant automated extraction or commercial redistribution rights.
- 条款/许可证：https://www.bundesliga.com/en/bundesliga/info/legal-notices, https://www.bundesliga.com/robots.txt
- Robots/access：robots/legal notice reserves text-and-data-mining rights; no automated fetch；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["bundesliga.com"], "max_response_bytes": 2097152, "path_policy": "policy inventory requires explicit adapter contract", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": null, "normal_minutes": null, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.bundesliga`；声明字段：`native_match_id, team_id, player_id, shirt_number, starter_or_bench, observed_at`；已观测：``；缺失：`native_match_id, team_id, player_id, shirt_number, starter_or_bench, observed_at`
- 实体联结：same competition + exact kickoff + explicit canonical home/away pair + native match ID; complete bilateral XI required
- 时间：observed_at must precede kickoff/freeze cutoff for model use; publication/effective time is not inferred from request time
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.bundesliga_official"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": null, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "bundesliga_official", "state": "rights_blocked"}`
- 替代方案：Operator-provided licensed official feed; until then no lineup confirmation and no inferred XI
- Eligibility：display=`blocked_pending_terms_review`，model=`blocked`，publication=`blocked`

### `official_laliga_lineups` — official_laliga_lineups

- Adapter：`league_platform.live_sources.laliga`；主机：`apim.laliga.com, laliga.com`
- 权利：first_party_access_reuse_terms_not_verified；The first-party page/API is publicly discoverable, but public visibility does not grant automated extraction or commercial redistribution rights.
- 条款/许可证：https://www.laliga.com/en-GB/legal/legal-web
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["apim.laliga.com", "laliga.com"], "max_response_bytes": 2097152, "path_policy": "policy inventory requires explicit adapter contract", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": null, "normal_minutes": null, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.laliga`；声明字段：`native_match_id, team_id, player_id, shirt_number, starter_or_bench, observed_at`；已观测：``；缺失：`native_match_id, team_id, player_id, shirt_number, starter_or_bench, observed_at`
- 实体联结：same competition + exact kickoff + explicit canonical home/away pair + native match ID; complete bilateral XI required
- 时间：observed_at must precede kickoff/freeze cutoff for model use; publication/effective time is not inferred from request time
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.laliga_official"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": null, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "laliga_official", "state": "rights_blocked"}`
- 替代方案：Operator-provided licensed official feed; until then no lineup confirmation and no inferred XI
- Eligibility：display=`blocked_pending_terms_review`，model=`blocked`，publication=`blocked`

### `official_league_lineups` — 英超/西甲/德甲/意甲/法甲官方比赛中心与首发

- Adapter：`league_platform.live_sources.{premier_league,laliga,bundesliga,seriea,ligue1}`；主机：`api-sdp.legaseriea.it, bundesliga.com, laliga.com, ma-api.ligue1.fr, premierleague.com`
- 权利：first_party_access_reuse_terms_not_verified；The first-party page/API is publicly discoverable, but public visibility does not grant automated extraction or commercial redistribution rights.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["premierleague.com", "laliga.com", "bundesliga.com", "api-sdp.legaseriea.it", "ma-api.ligue1.fr"], "max_response_bytes": 2097152, "path_policy": "public_https_strict_fixture_join", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 2, "normal_minutes": 30, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.{premier_league,laliga,bundesliga,seriea,ligue1}`；声明字段：`native_match_id, team_id, player_id, shirt_number, starter_or_bench, observed_at`；已观测：``；缺失：`native_match_id, team_id, player_id, shirt_number, starter_or_bench, observed_at`
- 实体联结：same competition + exact kickoff + explicit canonical home/away pair + native match ID; complete bilateral XI required
- 时间：observed_at must precede kickoff/freeze cutoff for model use; publication/effective time is not inferred from request time
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.official_league_lineups"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_observed", "status": "unavailable", "status_codes": []}`
- 状态/失败：`{"reason": "runtime payload or diagnostics are present; see errors and coverage", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "unavailable"}`
- 替代方案：Operator-provided licensed official feed; until then no lineup confirmation and no inferred XI
- Eligibility：display=`blocked_pending_terms_review`，model=`blocked`，publication=`blocked`

### `official_ligue1_lineups` — official_ligue1_lineups

- Adapter：`league_platform.live_sources.ligue1`；主机：`ligue1.com, ma-api.ligue1.fr`
- 权利：first_party_access_reuse_terms_not_verified；The first-party page/API is publicly discoverable, but public visibility does not grant automated extraction or commercial redistribution rights.
- 条款/许可证：https://ligue1.com/en/legal/cgu
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["ma-api.ligue1.fr", "ligue1.com"], "max_response_bytes": 2097152, "path_policy": "policy inventory requires explicit adapter contract", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": null, "normal_minutes": null, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.ligue1`；声明字段：`native_match_id, team_id, player_id, shirt_number, starter_or_bench, observed_at`；已观测：``；缺失：`native_match_id, team_id, player_id, shirt_number, starter_or_bench, observed_at`
- 实体联结：same competition + exact kickoff + explicit canonical home/away pair + native match ID; complete bilateral XI required
- 时间：observed_at must precede kickoff/freeze cutoff for model use; publication/effective time is not inferred from request time
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.ligue1_official"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": null, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "ligue1_official", "state": "rights_blocked"}`
- 替代方案：Operator-provided licensed official feed; until then no lineup confirmation and no inferred XI
- Eligibility：display=`blocked_pending_terms_review`，model=`blocked`，publication=`blocked`

### `official_premier_league_lineups` — official_premier_league_lineups

- Adapter：`league_platform.live_sources.premier_league`；主机：`premierleague.com`
- 权利：first_party_access_reuse_terms_not_verified；The first-party page/API is publicly discoverable, but public visibility does not grant automated extraction or commercial redistribution rights.
- 条款/许可证：https://www.premierleague.com/en/terms-and-conditions, https://www.premierleague.com/robots.txt
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["premierleague.com"], "max_response_bytes": 2097152, "path_policy": "policy inventory requires explicit adapter contract", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": null, "normal_minutes": null, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.premier_league`；声明字段：`native_match_id, team_id, player_id, shirt_number, starter_or_bench, observed_at`；已观测：``；缺失：`native_match_id, team_id, player_id, shirt_number, starter_or_bench, observed_at`
- 实体联结：same competition + exact kickoff + explicit canonical home/away pair + native match ID; complete bilateral XI required
- 时间：observed_at must precede kickoff/freeze cutoff for model use; publication/effective time is not inferred from request time
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.premier_league_official"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": null, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "premier_league_official", "state": "rights_blocked"}`
- 替代方案：Operator-provided licensed official feed; until then no lineup confirmation and no inferred XI
- Eligibility：display=`blocked_pending_terms_review`，model=`blocked`，publication=`blocked`

### `official_serie_a_lineups` — official_serie_a_lineups

- Adapter：`league_platform.live_sources.seriea`；主机：`api-sdp.legaseriea.it, en.legaseriea.it`
- 权利：first_party_access_reuse_terms_not_verified；The first-party page/API is publicly discoverable, but public visibility does not grant automated extraction or commercial redistribution rights.
- 条款/许可证：https://en.legaseriea.it/terms-and-conditions
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["api-sdp.legaseriea.it", "en.legaseriea.it"], "max_response_bytes": 2097152, "path_policy": "policy inventory requires explicit adapter contract", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": null, "normal_minutes": null, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.seriea`；声明字段：`native_match_id, team_id, player_id, shirt_number, starter_or_bench, observed_at`；已观测：``；缺失：`native_match_id, team_id, player_id, shirt_number, starter_or_bench, observed_at`
- 实体联结：same competition + exact kickoff + explicit canonical home/away pair + native match ID; complete bilateral XI required
- 时间：observed_at must precede kickoff/freeze cutoff for model use; publication/effective time is not inferred from request time
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.serie_a_official"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": null, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "serie_a_official", "state": "rights_blocked"}`
- 替代方案：Operator-provided licensed official feed; until then no lineup confirmation and no inferred XI
- Eligibility：display=`blocked_pending_terms_review`，model=`blocked`，publication=`blocked`

### `open_meteo` — Open-Meteo 天气与场地环境

- Adapter：`league_platform.live_sources.open_meteo`；主机：`api.open-meteo.com, geocoding-api.open-meteo.com`
- 权利：rights_unverified；No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["geocoding-api.open-meteo.com", "api.open-meteo.com"], "max_response_bytes": 2097152, "path_policy": "public_https_bounded_requests", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 30, "normal_minutes": 180, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.open_meteo`；声明字段：`provider_native_id, teams_or_entities, observed_fields, observed_at`；已观测：``；缺失：`provider_native_id, teams_or_entities, observed_fields, observed_at`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.open_meteo"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "rights_blocked"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_pending_rights_review`，model=`blocked`，publication=`blocked`

### `open_meteo_geocoding` — open_meteo_geocoding

- Adapter：`league_platform.live_sources.geocoding`；主机：`geocoding-api.open-meteo.com`
- 权利：free_tier_noncommercial_cc_by_only；Open-Meteo free API is non-commercial under its terms; commercial use requires an appropriate subscription/contract.
- 条款/许可证：https://open-meteo.com/en/licence, https://open-meteo.com/en/terms
- Robots/access：HTTPS API allowlist; free-tier limits and commercial plan boundary apply；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "bounded and cached", "free_tier_budget": "10,000/day; 5,000/hour; 600/minute per terms", "host_allowlist": ["geocoding-api.open-meteo.com"], "max_response_bytes": 5242880, "path_policy": "documented HTTPS API only", "redirects": "reject or fail closed", "timeout_seconds": 30}`
- 限速：`{"near_kickoff_minutes": 30, "normal_minutes": 180, "provider_rule": "free-tier daily/hourly/minute budgets; cache repeated coordinates"}`
- Parser：`league_platform.live_sources.geocoding`；声明字段：`place_query, geocoded_coordinates, provider_place_id, retrieved_at`；已观测：``；缺失：`place_query, geocoded_coordinates, provider_place_id, retrieved_at`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：retrieved_at is the observation clock for place resolution; coordinates require exact query/response provenance
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.geocoding"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": null, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "geocoding", "state": "rights_blocked"}`
- 替代方案：MET Norway with attribution and exact coordinates; self-hosted/licensed Open-Meteo plan for commercial use
- Eligibility：display=`blocked_for_current_commercial_release`，model=`blocked_pending_commercial_plan`，publication=`blocked_pending_commercial_plan`

### `open_meteo_weather` — open_meteo_weather

- Adapter：`league_platform.live_sources.open_meteo`；主机：`api.open-meteo.com`
- 权利：free_tier_noncommercial_cc_by_only；Open-Meteo free API is non-commercial under its terms; commercial use requires an appropriate subscription/contract.
- 条款/许可证：https://open-meteo.com/en/licence, https://open-meteo.com/en/terms
- Robots/access：HTTPS API allowlist; free-tier limits and commercial plan boundary apply；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "bounded and cached", "free_tier_budget": "10,000/day; 5,000/hour; 600/minute per terms", "host_allowlist": ["api.open-meteo.com"], "max_response_bytes": 5242880, "path_policy": "documented HTTPS API only", "redirects": "reject or fail closed", "timeout_seconds": 30}`
- 限速：`{"near_kickoff_minutes": 30, "normal_minutes": 180, "provider_rule": "free-tier daily/hourly/minute budgets; cache repeated coordinates"}`
- Parser：`league_platform.live_sources.open_meteo`；声明字段：`fixture_id, forecast_hour, temperature, precipitation, wind, retrieved_at`；已观测：``；缺失：`fixture_id, forecast_hour, temperature, precipitation, wind, retrieved_at`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：forecast_at is the target hour; retrieved_at is observation time; forecast must be within provider horizon and before freeze
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.weather"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": null, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "weather", "state": "rights_blocked"}`
- 替代方案：MET Norway with attribution and exact coordinates; self-hosted/licensed Open-Meteo plan for commercial use
- Eligibility：display=`blocked_for_current_commercial_release`，model=`blocked_pending_commercial_plan`，publication=`blocked_pending_commercial_plan`

### `openfootball_current` — OpenFootball 当前赛程与赛果

- Adapter：`league_platform.live_sources.openfootball_live`；主机：`raw.githubusercontent.com`
- 权利：verified_cc0_public_domain；OpenFootball public-domain/CC0 data; only fixed source files and durable raw admission are eligible.
- 条款/许可证：https://github.com/openfootball/football.json, https://github.com/openfootball/football.json/blob/master/LICENSE.md
- Robots/access：fixed raw URLs; no robots traversal; same-host HTTPS only；网络判定 `attempted_under_declared_policy`
- Allowlist/限制：`{"concurrency": "bounded per host", "host_allowlist": ["raw.githubusercontent.com"], "max_response_bytes": 10485760, "path_policy": "fixed operator URL set only", "redirects": "reject", "timeout_seconds": 30}`
- 限速：`{"near_kickoff_minutes": 10, "normal_minutes": 30, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.openfootball_live`；声明字段：`fixture_id, provider_team_id, competition, season, round, kickoff_at, score, halftime_score`；已观测：`competition, fixture_id, halftime_score, kickoff_at, round, score, season`；缺失：`provider_team_id`
- 实体联结：canonical OpenFootball fixture ID; provider-scoped team IDs; exact competition/team/time; no fuzzy join
- 时间：preserve effective_at when supplied and observed_at/retrieved_at; only exact timezone-aware kickoff rows enter causal fixture/result lanes
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 16, "observed_hashes": ["16fafb2278e99bf41f9aa6753e9e8ae19f8eb2702151fe1379ad07fc4abe41f3", "1feef8cdd2e7c3aeadbe05154083ec3f87cb1f8c90af6cd26b0d63f5711c9380", "2c1e960a181195800d0ebe7f3d12f0c375695ae31db96cfca7138eef6d6ad923", "623cf5bdfeb96cbb2b058e113ba17f800b789f72caefd4800ebb422a7c15a6d1", "650c2c835f82e629482cf8a94650c329f378b3035a7dea7b8028c5a4615fffd8", "7e7b077b87643bbe3bf1c7cf793148b1f18db9bc7eb65a769c4cf082293cbe58", "7f1f62eb0640f05a02650951b6bd8b69a3399ef9653c59e0b6528e4653028b62", "826a5bf648a4fe10d99bd7b03c96134f50658411e9cb973c9cfcc2753fdcedda"], "observed_hashes_truncated": true, "required": true}`
- 实际 HTTP/运行：`{"attempted": true, "content_bytes": 54565, "content_encoding": null, "content_sha256": "1feef8cdd2e7c3aeadbe05154083ec3f87cb1f8c90af6cd26b0d63f5711c9380", "content_types": ["text/plain; charset=utf-8"], "error_count": 0, "evidence": ["https://raw.githubusercontent.com/openfootball/football.json/master/2026-27/en.1.json"], "network_opened": true, "raw_hash_count": 16, "raw_hashes": ["16fafb2278e99bf41f9aa6753e9e8ae19f8eb2702151fe1379ad07fc4abe41f3", "1feef8cdd2e7c3aeadbe05154083ec3f87cb1f8c90af6cd26b0d63f5711c9380", "2c1e960a181195800d0ebe7f3d12f0c375695ae31db96cfca7138eef6d6ad923", "623cf5bdfeb96cbb2b058e113ba17f800b789f72caefd4800ebb422a7c15a6d1", "650c2c835f82e629482cf8a94650c329f378b3035a7dea7b8028c5a4615fffd8", "7e7b077b87643bbe3bf1c7cf793148b1f18db9bc7eb65a769c4cf082293cbe58", "7f1f62eb0640f05a02650951b6bd8b69a3399ef9653c59e0b6528e4653028b62", "826a5bf648a4fe10d99bd7b03c96134f50658411e9cb973c9cfcc2753fdcedda"], "raw_hashes_truncated": true, "record_count": null, "request_started_at": "2026-09-15T19:41:55.871267+00:00", "response_observed_at": "2026-09-15T19:41:56.531521+00:00", "source_id": "openfootball_current", "state": "success", "status": "ok", "status_codes": [200], "wire_bytes": 54565, "wire_sha256": "1feef8cdd2e7c3aeadbe05154083ec3f87cb1f8c90af6cd26b0d63f5711c9380"}`
- 状态/失败：`{"reason": "dated operator probe admitted a valid response", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "openfootball_current", "state": "fresh"}`
- 替代方案：OpenFootball fixed CC0 source files are the canonical schedule/result and historical fallback
- Eligibility：display=`conditional_after_admission`，model=`conditional_after_causal_gate`，publication=`conditional_after_maturity_gate`

### `openfootball_historical` — openfootball_historical

- Adapter：`league_platform.sources.openfootball_verified`；主机：`raw.githubusercontent.com`
- 权利：verified_cc0_public_domain；OpenFootball public-domain/CC0 data; only fixed source files and durable raw admission are eligible.
- 条款/许可证：https://github.com/openfootball/football.json, https://github.com/openfootball/football.json/blob/master/LICENSE.md
- Robots/access：fixed raw URLs; no robots traversal; same-host HTTPS only；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "bounded per host", "host_allowlist": ["raw.githubusercontent.com"], "max_response_bytes": 10485760, "path_policy": "fixed operator URL set only", "redirects": "reject", "timeout_seconds": 30}`
- 限速：`{"near_kickoff_minutes": null, "normal_minutes": null, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.sources.openfootball_verified`；声明字段：`fixture_id, provider_team_id, competition, season, round, kickoff_at, score, halftime_score`；已观测：``；缺失：`fixture_id, provider_team_id, competition, season, round, kickoff_at, score, halftime_score`
- 实体联结：canonical OpenFootball fixture ID; provider-scoped team IDs; exact competition/team/time; no fuzzy join
- 时间：preserve effective_at when supplied and observed_at/retrieved_at; only exact timezone-aware kickoff rows enter causal fixture/result lanes
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_types": [], "evidence": ["no runtime section for this policy-only source"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "state": "not_observed", "status": "not_observed_for_this_snapshot", "status_codes": []}`
- 状态/失败：`{"reason": "historical CC0 source is consumed through verified archive training lane", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "training_only"}`
- 替代方案：OpenFootball fixed CC0 source files are the canonical schedule/result and historical fallback
- Eligibility：display=`conditional_after_admission`，model=`conditional_after_causal_gate`，publication=`conditional_after_maturity_gate`

### `openligadb_secondary_results` — OpenLigaDB 赛果二次核验

- Adapter：`league_platform.live_sources.openligadb`；主机：`api.openligadb.de`
- 权利：verified_odbl_isolated_display_current；OpenLigaDB publishes an ODbL database/API; this project isolates it to exact-join current/post-match display and attribution.
- 条款/许可证：https://api.openligadb.de/index.html, https://www.openligadb.de/lizenz
- Robots/access：API allowlist; no redirect; no arbitrary endpoint discovery；网络判定 `attempted_under_declared_policy`
- Allowlist/限制：`{"concurrency": "bounded and lookback capped", "host_allowlist": ["api.openligadb.de"], "match_max_response_bytes": 1048576, "max_response_bytes": 10485760, "path_policy": "/getmatchdata/bl1/<season> or /getmatchdata/<numeric-match-id>", "redirects": "reject", "timeout_seconds": 20}`
- 限速：`{"near_kickoff_minutes": 10, "normal_minutes": 60, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.openligadb`；声明字段：`provider_match_id, provider_team_id, kickoff_at, finished_score, halftime_score, goals, venue`；已观测：`finished_score, goals, halftime_score, kickoff_at, provider_match_id, venue`；缺失：`provider_team_id`
- 实体联结：unique exact Bundesliga competition + UTC kickoff + home/away team join; unmatched/conflict rows isolated
- 时间：preserve effective_at when supplied and observed_at/retrieved_at; only exact timezone-aware kickoff rows enter causal fixture/result lanes
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 1, "observed_hashes": ["1d64d3f9473abf28dfc0db3f3e6a9f2257562982580774eefa32e10768156508"], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": true, "content_bytes": 278640, "content_encoding": null, "content_sha256": "1d64d3f9473abf28dfc0db3f3e6a9f2257562982580774eefa32e10768156508", "content_types": ["application/json; charset=utf-8"], "error_count": 0, "evidence": ["https://api.openligadb.de/getmatchdata/bl1/2026"], "network_opened": true, "raw_hash_count": 1, "raw_hashes": ["1d64d3f9473abf28dfc0db3f3e6a9f2257562982580774eefa32e10768156508"], "raw_hashes_truncated": false, "record_count": null, "request_started_at": "2026-09-15T19:41:56.531658+00:00", "response_observed_at": "2026-09-15T19:42:01.441288+00:00", "source_id": "openligadb_secondary_results", "state": "success", "status": "ok", "status_codes": [200], "wire_bytes": 278640, "wire_sha256": "1d64d3f9473abf28dfc0db3f3e6a9f2257562982580774eefa32e10768156508"}`
- 状态/失败：`{"reason": "dated operator probe admitted a valid response", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "openligadb", "state": "fresh"}`
- 替代方案：OpenFootball exact-kickoff canonical result; OpenLigaDB remains isolated display/post-match cross-check
- Eligibility：display=`allowed_isolated`，model=`blocked`，publication=`blocked`

### `osm_geodata` — osm_geodata

- Adapter：`future adapter; no executable module in current tree`；主机：`nominatim.openstreetmap.org, www.openstreetmap.org`
- 权利：rights_metadata_present_adapter_contract_pending；A possible license/terms reference exists, but the adapter, attribution and publication contract is not implemented in the current inventory.
- 条款/许可证：https://opendatacommons.org/licenses/odbl/1-0/, https://www.openstreetmap.org/copyright
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["www.openstreetmap.org", "nominatim.openstreetmap.org"], "max_response_bytes": 2097152, "path_policy": "policy inventory requires explicit adapter contract", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": null, "normal_minutes": null, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`future adapter; no executable module in current tree`；声明字段：`place_query, coordinates, provider_place_id, retrieved_at`；已观测：``；缺失：`place_query, coordinates, provider_place_id, retrieved_at`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：retrieved_at is the observation clock; coordinates are not time-varying match facts and remain field-level provenance
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_types": [], "evidence": ["no runtime section for this policy-only source"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "state": "not_observed", "status": "not_observed_for_this_snapshot", "status_codes": []}`
- 状态/失败：`{"reason": "policy inventory has no executable adapter/attribution contract", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "future_disabled"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_until_adapter_contract`，model=`blocked`，publication=`blocked`

### `pappalardo_wyscout_historical` — pappalardo_wyscout_historical

- Adapter：`future adapter; no executable module in current tree`；主机：`figshare.com`
- 权利：rights_metadata_present_adapter_contract_pending；A possible license/terms reference exists, but the adapter, attribution and publication contract is not implemented in the current inventory.
- 条款/许可证：https://creativecommons.org/licenses/by/4.0/, https://figshare.com/collections/Soccer_match_event_dataset/4415000
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["figshare.com"], "max_response_bytes": 2097152, "path_policy": "policy inventory requires explicit adapter contract", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": null, "normal_minutes": null, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`future adapter; no executable module in current tree`；声明字段：`fixture_or_match_id, teams, kickoff_or_date, score, historical_market_fields`；已观测：``；缺失：`fixture_or_match_id, teams, kickoff_or_date, score, historical_market_fields`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：historical source timestamp/season boundary is retained; rows cannot become current or future observations
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_types": [], "evidence": ["no runtime section for this policy-only source"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "state": "not_observed", "status": "not_observed_for_this_snapshot", "status_codes": []}`
- 状态/失败：`{"reason": "policy inventory has no executable adapter/attribution contract", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "future_disabled"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_until_adapter_contract`，model=`blocked`，publication=`blocked`

### `premier_league_public_pages` — 英超官方公开比赛页

- Adapter：`league_platform.live_sources.crawl4ai`；主机：`www.premierleague.com`
- 权利：first_party_access_reuse_terms_not_verified；The first-party page/API is publicly discoverable, but public visibility does not grant automated extraction or commercial redistribution rights.
- 条款/许可证：https://www.premierleague.com/en/terms-and-conditions, https://www.premierleague.com/robots.txt
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["www.premierleague.com"], "max_response_bytes": 2097152, "path_policy": "explicit_path_robots_allowlist_required", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 10, "normal_minutes": 60, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.crawl4ai`；声明字段：`provider_native_id, teams_or_entities, observed_fields, observed_at`；已观测：``；缺失：`provider_native_id, teams_or_entities, observed_fields, observed_at`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.premier_league_public_pages"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_observed", "status": "unavailable", "status_codes": []}`
- 状态/失败：`{"reason": "runtime payload or diagnostics are present; see errors and coverage", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "unavailable"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_pending_terms_review`，model=`blocked`，publication=`blocked`

### `public_rss_news` — BBC/Sky/Google News RSS 事实线索

- Adapter：`league_platform.live_sources.news`；主机：`feeds.bbci.co.uk, news.google.com, www.skysports.com`
- 权利：rights_unverified；No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["feeds.bbci.co.uk", "www.skysports.com", "news.google.com"], "max_response_bytes": 2097152, "path_policy": "public_https_rss_only", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 10, "normal_minutes": 30, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.news`；声明字段：`article_url, title, published_at, team_or_fixture_reference, content_hash`；已观测：``；缺失：`article_url, title, published_at, team_or_fixture_reference, content_hash`
- 实体联结：explicit fixture/team reference and article timestamp; otherwise global source-health evidence only
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.news"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "news", "state": "rights_blocked"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_pending_rights_review`，model=`blocked`，publication=`blocked`

### `seriea_public_pages` — 意甲官方公开比赛页

- Adapter：`league_platform.live_sources.crawl4ai`；主机：`en.legaseriea.it`
- 权利：first_party_access_reuse_terms_not_verified；The first-party page/API is publicly discoverable, but public visibility does not grant automated extraction or commercial redistribution rights.
- 条款/许可证：https://en.legaseriea.it/terms-and-conditions
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["en.legaseriea.it"], "max_response_bytes": 2097152, "path_policy": "explicit_path_robots_allowlist_required", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 10, "normal_minutes": 60, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.crawl4ai`；声明字段：`provider_native_id, teams_or_entities, observed_fields, observed_at`；已观测：``；缺失：`provider_native_id, teams_or_entities, observed_fields, observed_at`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.seriea_public_pages"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_observed", "status": "unavailable", "status_codes": []}`
- 状态/失败：`{"reason": "runtime payload or diagnostics are present; see errors and coverage", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "unavailable"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_pending_terms_review`，model=`blocked`，publication=`blocked`

### `sevenm_csl_public_fixture_script` — sevenm_csl_public_fixture_script

- Adapter：`league_platform.sources.sevenm_csl`；主机：`data.7m.com.cn`
- 权利：rights_unverified；No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["data.7m.com.cn"], "max_response_bytes": 2097152, "path_policy": "policy inventory requires explicit adapter contract", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": null, "normal_minutes": null, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.sources.sevenm_csl`；声明字段：`fixture_or_match_id, teams, kickoff_or_date, score, historical_market_fields`；已观测：``；缺失：`fixture_or_match_id, teams, kickoff_or_date, score, historical_market_fields`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_types": [], "evidence": ["no runtime section for this policy-only source"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "state": "not_observed", "status": "not_observed_for_this_snapshot", "status_codes": []}`
- 状态/失败：`{"reason": "no source section was present in this runtime snapshot", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "not_observed"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_pending_rights_review`，model=`blocked`，publication=`blocked`

### `sofascore_prematch` — SofaScore 赛前事件、伤停与阵容线索

- Adapter：`league_platform.live_sources.sofascore`；主机：`api.sofascore.com`
- 权利：rights_unverified；No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["api.sofascore.com"], "max_response_bytes": 2097152, "path_policy": "public_https_bounded_requests", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 5, "normal_minutes": 30, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.sofascore`；声明字段：`provider_native_id, teams_or_entities, observed_fields, observed_at`；已观测：``；缺失：`provider_native_id, teams_or_entities, observed_fields, observed_at`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.sofascore"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "sofascore", "state": "rights_blocked"}`
- 替代方案：OpenFootball for exact fixture/result facts; obtain a documented license or keep this field unavailable
- Eligibility：display=`blocked_pending_rights_review`，model=`blocked`，publication=`blocked`

### `sofifa_public_reference` — SoFIFA 公共球员参考

- Adapter：`league_platform.live_sources.crawl4ai`；主机：`sofifa.com`
- 权利：robots_policy_blocked；The current policy records a robots/API restriction; no network request is opened until that policy changes.
- 条款/许可证：https://sofifa.com/robots.txt
- Robots/access：robots/API path blocked; no request is opened；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["sofifa.com"], "max_response_bytes": 2097152, "path_policy": "robots_api_disallowed", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 0, "normal_minutes": 0, "provider_rule": "polling disabled before network by rights/robots gate"}`
- Parser：`league_platform.live_sources.crawl4ai`；声明字段：`provider_native_id, teams_or_entities, observed_fields, observed_at`；已观测：``；缺失：`provider_native_id, teams_or_entities, observed_fields, observed_at`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.sofifa_public_reference"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "blocked_by_robots", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "blocked_by_robots"}`
- 替代方案：Use OpenFootball historical/current facts or obtain provider permission; do not bypass robots/WAF/TLS policy
- Eligibility：display=`blocked`，model=`blocked`，publication=`blocked`

### `sports_lottery_official` — 中国体育彩票公开赛程与赔率

- Adapter：`league_platform.live_sources.sports_lottery`；主机：`webapi.sporttery.cn`
- 权利：rights_unverified；No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["webapi.sporttery.cn"], "max_response_bytes": 2097152, "path_policy": "public_https_no_redirect_no_proxy", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 5, "normal_minutes": 30, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.sports_lottery`；声明字段：`native_match_id, market, line, price, observed_at, settlement_rule`；已观测：``；缺失：`native_match_id, market, line, price, observed_at, settlement_rule`
- 实体联结：exact competition + UTC kickoff + canonical home/away identity; low-confidence rows remain display-only
- 时间：quote observed_at and settlement line are mandatory; stale/history rows cannot become current odds
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.sports_lottery"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "sports_lottery", "state": "rights_blocked"}`
- 替代方案：Licensed market feed with explicit redistribution/settlement rights; otherwise market baseline stays unavailable
- Eligibility：display=`blocked_pending_rights_review`，model=`blocked`，publication=`blocked`

### `understat_xg` — Understat 近期 xG 与球队状态

- Adapter：`league_platform.live_sources.understat`；主机：`understat.com`
- 权利：rights_unverified；No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.
- 条款/许可证：未声明；因此不放行
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["understat.com"], "max_response_bytes": 2097152, "path_policy": "public_https_bounded_requests", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 60, "normal_minutes": 360, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.understat`；声明字段：`team_or_match_id, observed_at, xg_or_rating, source_payload_hash`；已观测：``；缺失：`team_or_match_id, observed_at, xg_or_rating, source_payload_hash`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.understat"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_attempted", "status": "rights_blocked", "status_codes": []}`
- 状态/失败：`{"reason": "runtime preserved an explicit policy/quarantine block", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "understat", "state": "rights_blocked"}`
- 替代方案：Use OpenFootball historical/current facts or obtain provider permission; do not bypass robots/WAF/TLS policy
- Eligibility：display=`blocked_pending_rights_review`，model=`blocked`，publication=`blocked`

### `whoscored_public_pages` — WhoScored 公开比赛预览页

- Adapter：`league_platform.live_sources.crawl4ai`；主机：`www.whoscored.com`
- 权利：rights_unverified；No explicit current policy grant for automated collection and product reuse is recorded; the source remains blocked or quarantined.
- 条款/许可证：https://www.whoscored.com/termsofuse
- Robots/access：source-specific robots/access proof is not independently archived; central rights gate applies before network；网络判定 `not_attempted`
- Allowlist/限制：`{"concurrency": "serial per host; bounded cross-host fan-out", "host_allowlist": ["www.whoscored.com"], "max_response_bytes": 2097152, "path_policy": "explicit_path_robots_allowlist_required", "redirects": "reject unless exact same-host redirect is explicitly contracted", "timeout_seconds": 45}`
- 限速：`{"near_kickoff_minutes": 0, "normal_minutes": 0, "provider_rule": "source-specific documented limit not independently verified; bounded operator schedule and no retry storm"}`
- Parser：`league_platform.live_sources.crawl4ai`；声明字段：`provider_native_id, teams_or_entities, observed_fields, observed_at`；已观测：``；缺失：`provider_native_id, teams_or_entities, observed_fields, observed_at`
- 实体联结：provider-native identity plus exact competition/team/time where available; ambiguity quarantined
- 时间：observed_at is mandatory; missing effective time is explicitly non-model and non-publication
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 0, "observed_hashes": [], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"attempted": false, "content_type_observation": "not_recorded_by_adapter", "content_types": [], "error_count": 0, "errors": [], "evidence": ["snapshot.runtime.whoscored_public_pages"], "network_opened": false, "raw_hash_count": 0, "raw_hashes": [], "raw_hashes_truncated": false, "record_count": null, "reported_record_count": 0, "state": "not_observed", "status": "unavailable", "status_codes": []}`
- 状态/失败：`{"reason": "runtime payload or diagnostics are present; see errors and coverage", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": null, "state": "unavailable"}`
- 替代方案：Use OpenFootball historical/current facts or obtain provider permission; do not bypass robots/WAF/TLS policy
- Eligibility：display=`blocked_pending_rights_review`，model=`blocked`，publication=`blocked`

### `wikidata_entities` — Wikidata 结构化球队与场馆实体

- Adapter：`league_platform.live_sources.wikidata`；主机：`www.wikidata.org`
- 权利：verified_cc0_structured_data；Wikidata structured data is CC0; entity/venue use remains bounded and exact-join only.
- 条款/许可证：https://www.wikidata.org/wiki/Wikidata:Data_access/en, https://www.wikidata.org/wiki/Wikidata:Licensing
- Robots/access：Wikidata access guidance: identifying User-Agent, bounded concurrency, stop on 429；网络判定 `attempted_under_declared_policy`
- Allowlist/限制：`{"concurrency": "serial bounded entity chain", "default_requests_per_call": 16, "host_allowlist": ["www.wikidata.org"], "max_requests_per_call": 96, "max_response_bytes": 2097152, "path_policy": "/w/api.php only", "redirects": "reject", "timeout_seconds": 30}`
- 限速：`{"near_kickoff_minutes": 60, "normal_minutes": 360, "provider_rule": "meaningful User-Agent; <=3 concurrent recommended; honor Retry-After"}`
- Parser：`league_platform.live_sources.wikidata`；声明字段：`team_entity_id, preferred_venue_entity_id, venue_name, latitude, longitude`；已观测：`preferred_venue_entity_id`；缺失：`team_entity_id, venue_name, latitude, longitude`
- 实体联结：team name search -> one senior club entity -> preferred home venue -> valid coordinates; ties quarantined
- 时间：retrieved_at is the observation clock; coordinates are not time-varying match facts and remain field-level provenance
- 原始哈希：`{"algorithm": "SHA-256", "deduplicate": "same raw bytes deduplicated; conflicting payloads retained", "missing_hash_meaning": "not_recorded_by_adapter_or_no_payload; never infer a hash", "observed_hash_count": 5, "observed_hashes": ["1b617dcce5ac91f9825f23d9aaeeef6029aa8b393c70f834aa003547729e2d40", "2ff669629ea4b14af4da0f29182e375e05c0d95831d6b0b20c22e7afba318872", "86a69c2e936cc8921c4df085d6d1567ea811454f54786133b484e5cce526a0b8", "acb1483457b41c072ace1528eba1efa5343be995f69a302e5b781bafb43bcc93", "d01a3a9c89b5df5caf89542a175d99a9011c7a75b28c4e1580b31bb8580d734a"], "observed_hashes_truncated": false, "required": true}`
- 实际 HTTP/运行：`{"adapter_run": {"error_count": 0, "model_eligible": false, "network_opened": true, "observed_at": "2026-09-15T18:17:21.326847+00:00", "reason": "medium-confidence venue coordinates are display-only", "record_count": 2, "request_count": 6, "status": "ok", "venue_entities": ["Q1675996", "Q720983"], "venue_raw_sha256": ["86a69c2e936cc8921c4df085d6d1567ea811454f54786133b484e5cce526a0b8", "acb1483457b41c072ace1528eba1efa5343be995f69a302e5b781bafb43bcc93"]}, "attempted": true, "content_bytes": 508, "content_encoding": "gzip", "content_sha256": "1b617dcce5ac91f9825f23d9aaeeef6029aa8b393c70f834aa003547729e2d40", "content_types": ["application/json; charset=utf-8"], "error_count": 0, "evidence": ["https://www.wikidata.org/w/api.php?action=wbsearchentities&search=FC%20Utrecht&language=en&format=json&limit=1"], "network_opened": true, "raw_hash_count": 5, "raw_hashes": ["1b617dcce5ac91f9825f23d9aaeeef6029aa8b393c70f834aa003547729e2d40", "2ff669629ea4b14af4da0f29182e375e05c0d95831d6b0b20c22e7afba318872", "86a69c2e936cc8921c4df085d6d1567ea811454f54786133b484e5cce526a0b8", "acb1483457b41c072ace1528eba1efa5343be995f69a302e5b781bafb43bcc93", "d01a3a9c89b5df5caf89542a175d99a9011c7a75b28c4e1580b31bb8580d734a"], "raw_hashes_truncated": false, "record_count": 1, "request_started_at": "2026-09-15T18:55:27.612759+00:00", "response_observed_at": "2026-09-15T18:55:33.970425+00:00", "source_id": "wikidata_entities", "state": "success", "status": "ok", "status_codes": [200], "wire_bytes": 268, "wire_sha256": "6abac3b8d343bd255592a74a271b064303a0ac65006d07df23864ac56f7cb9a1"}`
- 状态/失败：`{"reason": "dated operator probe admitted a valid response", "runtime_error_count": 0, "runtime_errors": [], "runtime_key": "wikidata_entities", "state": "fresh"}`
- 替代方案：Use the other permitted CC0/CC-BY lane only for its own fields; keep missing venue/weather explicit
- Eligibility：display=`allowed_with_attribution_preference`，model=`conditional_exact_venue_chain`，publication=`conditional_with_provenance`
