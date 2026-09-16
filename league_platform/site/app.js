/* ============================================================================
   Matchline · 六联赛模型审计台
   数据契约见 league_platform/domain.py 与 docs/current-data-and-prediction-contract.md
   ========================================================================= */

const state = {
  snapshot: null,
  predictions: null,
  blockedFixtures: new Set(),
  competition: "all",
  season: "all",
  status: "all",
  date: "",
  query: "",
  visible: 30,
  activeMatchId: null,
};

const STATUS_LABELS = {
  finished: "完赛",
  upcoming: "未赛",
  live: "进行中",
  postponed: "延期",
  cancelled: "取消",
};

const SOURCE_STATUS_LABELS = {
  fresh: "新鲜",
  delayed: "延迟",
  degraded: "降级",
  stale: "过期",
  unavailable: "不可用",
  not_requested: "未到窗口",
  not_configured: "未配置",
  blocked_by_robots: "robots 阻断",
  forbidden: "禁止执行",
  quarantined: "已隔离",
  opt_in: "需显式启用",
  training_only: "仅历史训练",
};

const GATE_LABELS = {
  research_only_underperforms_market: "研究对照 · 弱于市场",
  research_only_no_market_baseline: "研究对照 · 无市场基线",
  research_predictions_available_production_blocked: "研究对照 · 生产阻断",
  historical_baselines_evaluated_current_predictions_blocked: "历史基线已评估 · 当前预测阻断",
  blocked_until_walk_forward_validation: "待滚动验证",
};

const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

const formatNumber = (value) => new Intl.NumberFormat("zh-CN").format(value);

function formatDate(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }).format(
    new Date(value),
  );
}

function formatTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }).format(
    new Date(value),
  );
}

function formatDateTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(value));
}

function timeUntil(value) {
  const delta = new Date(value).getTime() - Date.now();
  if (delta <= 0) return "已开赛";
  const days = Math.floor(delta / 86400000);
  const hours = Math.floor((delta % 86400000) / 3600000);
  if (days > 1) return `${days} 天后`;
  if (days === 1) return "明天";
  if (hours > 0) return `${hours} 小时后`;
  return "即将开赛";
}

const leagueById = (id) => state.snapshot.competitions.find((league) => league.id === id);

/* ------------------------------------------------------------ data helpers */

function evalRow(league) {
  // The offline bundle may carry the expanded strict walk-forward report in
  // addition to the legacy compact evaluation. Prefer the strict report so
  // the audit UI cannot silently fall back to the old three-season sample.
  const mh = league.strict_model_health || league.model_health || {};
  const baselines = mh.baselines || {};
  return {
    id: league.id,
    nameZh: league.name_zh,
    selected: mh.selected_candidate || mh.model || "—",
    gate: mh.quality_gate || mh.status || "unknown",
    metrics: mh.selected_metrics || {},
    market: baselines.market || null,
    historical: baselines.historical_frequency || null,
    folds: mh.walk_forward_folds || [],
    evaluationSeason: mh.evaluation_season,
    dataCutoff: mh.data_cutoff,
  };
}

/** Sample-weighted mean of one metric across leagues. */
function weightedMean(rows, pick, key = "brier_score") {
  let numerator = 0;
  let denominator = 0;
  for (const row of rows) {
    const metrics = pick(row);
    if (!metrics || typeof metrics[key] !== "number" || !metrics.sample_n) continue;
    numerator += metrics[key] * metrics.sample_n;
    denominator += metrics.sample_n;
  }
  return denominator ? { value: numerator / denominator, sampleN: denominator } : null;
}

function aggregateStats(rows) {
  const withMarket = rows.filter((row) => row.market);
  const model = weightedMean(withMarket, (row) => row.metrics);
  const market = weightedMean(withMarket, (row) => row.market);
  const history = weightedMean(withMarket, (row) => row.historical);
  const modelAll = weightedMean(rows, (row) => row.metrics);
  return {
    model,
    market,
    history,
    modelAll,
    delta: model && market ? model.value - market.value : null,
    relative: model && market ? (model.value - market.value) / market.value : null,
    vsHistory: model && history ? (history.value - model.value) / history.value : null,
    beatsMarket: withMarket.filter((row) => row.metrics.brier_score < row.market.brier_score).length,
    beatsHistory: rows.filter(
      (row) => row.historical && row.metrics.brier_score < row.historical.brier_score,
    ).length,
    withMarket: withMarket.length,
    total: rows.length,
    modelName: rows.find((row) => row.selected && row.selected !== "—")?.selected || "—",
  };
}

/* ------------------------------------------------------------ hero & tiles */

function renderHero(rows) {
  const stats = aggregateStats(rows);
  const figure = $("#hero-figure");
  const behind = stats.relative > 0;

  figure.style.color = behind ? "var(--text-serious)" : "var(--text-good)";
  figure.innerHTML = `<span class="num">${behind ? "+" : ""}${(stats.relative * 100).toFixed(1)}%</span><small>Brier 相对市场</small>`;

  $("#overview-title").textContent = behind
    ? "模型仍然读不过市场"
    : "模型在加权口径下好于市场";
  $("#hero-lede").textContent = behind
    ? `在 ${stats.withMarket} 个有市场基线的联赛、共 ${formatNumber(stats.model.sampleN)} 场留出比赛上，模型的加权 Brier 比市场高 ${(stats.relative * 100).toFixed(1)}%。领先市场的联赛：${stats.beatsMarket} 个。`
    : `在 ${stats.withMarket} 个有市场基线的联赛上，模型的加权 Brier 低于市场 ${Math.abs(stats.relative * 100).toFixed(1)}%。`;

  $("#hero-notes").innerHTML = [
    ["model", "模型", stats.model, stats.modelName],
    ["market", "市场基准", stats.market, "博彩去水概率"],
    ["baseline", "历史频率", stats.history, "赛季主平客频率"],
  ]
    .filter(([, , metric]) => metric)
    .map(
      ([token, label, metric, note]) => `
      <div class="hero-note">
        <svg viewBox="0 0 24 24" aria-hidden="true" style="stroke:var(--${token})">
          <circle cx="12" cy="12" r="7.5" />
        </svg>
        <span>${label} Brier <b class="num">${metric.value.toFixed(3)}</b> · ${escapeHtml(note)}</span>
      </div>`,
    )
    .join("") +
    `<div class="hero-note">
       <svg viewBox="0 0 24 24" aria-hidden="true" style="stroke:var(--text-good)">
         <path d="M4 12.5l5 5L20 6.5" stroke-linecap="round" stroke-linejoin="round" />
       </svg>
       <span>但 ${stats.beatsHistory}/${stats.total} 个联赛都赢过历史频率，整体降低 ${(stats.vsHistory * 100).toFixed(1)}%</span>
     </div>`;

  const summary = state.snapshot.summary;
  $("#tile-matches").textContent = formatNumber(summary.finished_matches);
  $("#tile-matches-foot").textContent = `训练与回测样本 · ${rows.length} 个联赛`;
  $("#tile-fixtures").textContent = formatNumber(summary.current_fixture_count ?? 0);
  $("#tile-fixtures-foot").textContent = `含 xG 球队 ${formatNumber(summary.current_xg_team_count ?? 0)} 支`;
  $("#tile-models").innerHTML = `${summary.evaluated_models ?? 0}<small> / ${rows.length}</small>`;
  $("#tile-models-foot").textContent = `留出赛季 ${rows[0]?.evaluationSeason ?? "—"} · 每季 4 折`;
  const prospective = state.snapshot.prospective_evaluation || {};
  const strictProductionAllowed = state.snapshot.strict_backtest?.overall?.production_allowed === true;
  const prospectivePassed = prospective.status === "passed";
  const gateOpen = strictProductionAllowed && prospectivePassed;
  const pending = Number.isFinite(Number(prospective.pending_n)) ? Number(prospective.pending_n) : null;
  $("#tile-gate").textContent = gateOpen ? "开放" : "关闭";
  $("#tile-gate").style.color = gateOpen ? "var(--text-good)" : "var(--text-warning)";
  $("#tile-gate-foot").textContent = gateOpen
    ? "严格报告与前瞻评估均通过"
    : pending != null && pending > 0
      ? `前瞻待评分 ${formatNumber(pending)} · 研究对照`
      : "严格报告或前瞻评估未通过";
}

/* ------------------------------------------------------- calibration ladder */

function renderLadder(rows) {
  const numeric = (value) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const ladderRows = rows.filter((row) => numeric(row?.metrics?.brier_score) !== null);
  const values = ladderRows
    .flatMap((row) => [row.metrics?.brier_score, row.market?.brier_score, row.historical?.brier_score])
    .map(numeric)
    .filter((value) => value !== null);
  if (!ladderRows.length || !values.length) {
    $("#ladder").innerHTML = '<div class="empty-state">暂无可用的三分类评估数据</div>';
    $("#ladder-scale").innerHTML = "";
    $("#ladder-foot-note").textContent = "待留出评估完成后显示；不会用缺失值补齐";
    return;
  }
  const lo = Math.floor(Math.min(...values) * 100) / 100;
  const hi = Math.ceil(Math.max(...values) * 100) / 100;
  const span = hi - lo || 1;
  const pos = (value) => {
    const parsed = numeric(value);
    return parsed === null ? null : ((parsed - lo) / span) * 100;
  };

  $("#ladder").innerHTML = ladderRows
    .slice()
    .sort((a, b) => numeric(a.metrics.brier_score) - numeric(b.metrics.brier_score))
    .map((row) => {
      const modelBrier = numeric(row.metrics.brier_score);
      const marketBrier = numeric(row.market?.brier_score);
      const historyBrier = numeric(row.historical?.brier_score);
      const xModel = pos(modelBrier);
      const xMarket = marketBrier === null ? null : pos(marketBrier);
      const xHistory = historyBrier === null ? null : pos(historyBrier);
      const delta = marketBrier === null ? null : modelBrier - marketBrier;
      const relative = marketBrier === null || marketBrier === 0 ? null : (delta / marketBrier) * 100;

      const deltaClass =
        relative === null ? "ladder-delta--none" : delta > 0 ? "ladder-delta--worse" : "ladder-delta--better";
      const deltaText =
        relative === null ? "无市场基线" : `${delta > 0 ? "+" : ""}${relative.toFixed(1)}%`;

      const gapStyle =
        xMarket === null
          ? "display:none"
          : `left:${Math.min(xMarket, xModel)}%;width:${Math.abs(xModel - xMarket)}%`;

      const label = relative === null
        ? `${row.nameZh}，模型 Brier ${modelBrier.toFixed(3)}，无可用市场基线`
        : `${row.nameZh}，模型 Brier ${modelBrier.toFixed(3)}，市场 ${marketBrier.toFixed(3)}，相对差 ${relative.toFixed(1)}%`;

      return `
        <div class="ladder-row" role="listitem" tabindex="0" data-league="${escapeHtml(row.id)}" aria-label="${escapeHtml(label)}">
          <span class="ladder-name">${escapeHtml(row.nameZh)}</span>
          <span class="ladder-track" aria-hidden="true">
            <span class="ladder-axis-line"></span>
            <span class="ladder-gap" style="${gapStyle}"></span>
            ${xHistory === null ? "" : `<span class="ladder-mark ladder-mark--baseline" style="left:${xHistory}%"></span>`}
            ${xMarket === null ? "" : `<span class="ladder-mark ladder-mark--market" style="left:${xMarket}%"></span>`}
            <span class="ladder-mark ladder-mark--model" style="left:${xModel}%"></span>
          </span>
          <span class="ladder-delta ${deltaClass} num">${deltaText}</span>
        </div>
      `;
    })
    .join("");

  const ticks = 5;
  $("#ladder-scale").innerHTML = Array.from({ length: ticks }, (_, index) => {
    const fraction = index / (ticks - 1);
    const value = lo + (hi - lo) * fraction;
    const clamp = index === 0 ? "left:0;transform:none" : index === ticks - 1 ? "left:100%;transform:translateX(-100%)" : `left:${fraction * 100}%`;
    return `<span style="${clamp}">${value.toFixed(2)}</span>`;
  }).join("");

  $("#ladder-foot-note").textContent = `Brier 分数（三分类，越低越好）· 留出赛季 ${rows[0]?.evaluationSeason ?? "—"}`;
  attachLadderTooltips(rows);
}

function renderMetricTable(rows) {
  $("#metric-table-body").innerHTML = rows
    .map((row) => {
      const evaluators = [
        ["模型", row.metrics],
        ["市场", row.market],
        ["历史频率", row.historical],
      ].filter((entry) => entry[1]);

      const bestName = (key) =>
        evaluators.reduce((best, entry) =>
          entry[1][key] < best[1][key] ? entry : best,
        )[0];

      const cell = (entry, key) => {
        const value = entry[1][key];
        if (typeof value !== "number") return '<td class="n">—</td>';
        const isBest = bestName(key) === entry[0];
        return `<td class="n${isBest ? " best" : ""}">${value.toFixed(3)}</td>`;
      };

      return evaluators
        .map(
          (entry, index) => `
          <tr>
            ${index === 0 ? `<th scope="row" rowspan="${evaluators.length}">${escapeHtml(row.nameZh)}</th>` : ""}
            <td>${entry[0]}${entry[0] === "模型" ? `<br><span style="color:var(--ink-3);font-size:11px">${escapeHtml(row.selected)}</span>` : ""}</td>
            <td class="n">${formatNumber(entry[1].sample_n ?? 0)}</td>
            ${cell(entry, "brier_score")}
            ${cell(entry, "log_loss")}
            ${cell(entry, "rps")}
            ${cell(entry, "ece")}
            ${index === 0 ? `<td rowspan="${evaluators.length}">${escapeHtml(GATE_LABELS[row.gate] ?? row.gate)}</td>` : ""}
          </tr>
        `,
        )
        .join("");
    })
    .join("");
}

/* ------------------------------------------------------- walk-forward folds */

/**
 * One panel per league. The y-scale is shared across every panel so the
 * fold lines are comparable between leagues, and the market baseline is a
 * labelled reference rule rather than a decorative line.
 */
function foldChart(row, scale) {
  const folds = row.folds;
  const width = 240;
  const height = 58;
  const pad = { l: 6, r: 6, t: 8, b: 8 };
  const x = (index) => pad.l + (index / (folds.length - 1)) * (width - pad.l - pad.r);
  const y = (value) =>
    pad.t + (1 - (value - scale.min) / (scale.max - scale.min)) * (height - pad.t - pad.b);
  const points = folds.map((fold, index) => [x(index), y(fold.brier_score)]);
  const path = points.map(([px, py], index) => `${index ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`).join(" ");
  const marketY = row.market ? y(row.market.brier_score) : null;

  return `
    <svg viewBox="0 0 ${width} ${height}" role="img"
         aria-label="${escapeHtml(row.nameZh)} 四折 Brier 走势，范围 ${scale.min.toFixed(2)} 到 ${scale.max.toFixed(2)}">
      ${
        marketY === null
          ? ""
          : `<line x1="${pad.l}" y1="${marketY.toFixed(1)}" x2="${width - pad.r}" y2="${marketY.toFixed(1)}"
                   stroke="var(--market)" stroke-width="1.25" />`
      }
      <path d="${path}" fill="none" stroke="var(--model)" stroke-width="1.75"
            stroke-linecap="round" stroke-linejoin="round" />
      ${points
        .map(
          ([px, py], index) =>
            `<circle data-league="${escapeHtml(row.id)}" data-fold="${folds[index].fold}"
                     cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="3.4"
                     fill="var(--model)" stroke="var(--sunken)" stroke-width="1.6" />`,
        )
        .join("")}
    </svg>
  `;
}

function renderFolds(rows) {
  const values = rows.flatMap((row) => [
    ...row.folds.map((fold) => fold.brier_score),
    ...(row.market ? [row.market.brier_score] : []),
  ]);
  const scale = {
    min: Math.floor(Math.min(...values) * 50) / 50,
    max: Math.ceil(Math.max(...values) * 50) / 50,
  };

  $("#folds").innerHTML = rows
    .map((row) => {
      if (row.folds.length < 2) {
        return `
          <div class="fold-cell">
            <div class="fold-top"><span class="fold-name">${escapeHtml(row.nameZh)}</span></div>
            <p style="color:var(--ink-2);font-size:11.5px">该联赛没有可展示的滚动验证折。</p>
          </div>`;
      }
      const foldValues = row.folds.map((fold) => fold.brier_score);
      const spread = Math.max(...foldValues) - Math.min(...foldValues);
      const worst = Math.max(...foldValues);
      const best = Math.min(...foldValues);
      const first = row.folds[0];
      const last = row.folds[row.folds.length - 1];
      return `
        <div class="fold-cell">
          <div class="fold-top">
            <span class="fold-name">${escapeHtml(row.nameZh)}</span>
            <span class="fold-spread">最好 ${best.toFixed(3)} · 最差 ${worst.toFixed(3)}</span>
          </div>
          ${foldChart(row, scale)}
          <div class="fold-axis">
            <span>${formatDate(first.start_at)}</span>
            <span class="fold-legend">${
              row.market
                ? `<i class="fold-rule" aria-hidden="true"></i>市场 ${row.market.brier_score.toFixed(3)}`
                : "无市场基准"
            }</span>
            <span>${formatDate(last.end_at)}</span>
          </div>
        </div>`;
    })
    .join("");

  $("#folds-scale").textContent = `所有面板共用纵轴 ${scale.min.toFixed(2)}–${scale.max.toFixed(2)}（Brier，越低越好）`;
}

/* ----------------------------------------------------------------- fixtures */

function filteredMatches() {
  return state.snapshot.matches
    .filter((match) => {
      if (state.competition !== "all" && match.competition_id !== state.competition) return false;
      if (state.season !== "all" && match.season !== state.season) return false;
      if (state.status !== "all" && match.status !== state.status) return false;
      if (state.date && match.kickoff_at.slice(0, 10) !== state.date) return false;
      if (state.query) {
        const query = state.query.toLocaleLowerCase("zh-CN");
        return (
          match.home_team.toLocaleLowerCase("zh-CN").includes(query) ||
          match.away_team.toLocaleLowerCase("zh-CN").includes(query)
        );
      }
      return true;
    })
    .sort((left, right) => {
      const leftUpcoming = left.status === "upcoming";
      const rightUpcoming = right.status === "upcoming";
      if (leftUpcoming !== rightUpcoming) return leftUpcoming ? -1 : 1;
      const direction = leftUpcoming ? 1 : -1;
      return direction * left.kickoff_at.localeCompare(right.kickoff_at);
    });
}

const FREEZE_STAGE_LABELS = {
  t_minus_24h: "24 小时",
  t_minus_6h: "6 小时",
  t_minus_90m: "90 分钟",
  lineup_confirmation: "官方首发",
};

function freezeTimeline(prediction) {
  const versions = prediction?.freeze_versions;
  if (!Array.isArray(versions) || !versions.length) return "";
  const stages = versions
    .map((version) => {
      const status = version.status === "available" ? "available" : "pending";
      const label = FREEZE_STAGE_LABELS[version.stage] ?? version.stage;
      const detail = version.status === "available" ? formatDateTime(version.cutoff_at) : "待到达";
      return `<span class="freeze-stage freeze-stage--${status}" title="${escapeHtml(`${label} · ${detail}`)}">
        <i aria-hidden="true"></i><b>${escapeHtml(label)}</b>
      </span>`;
    })
    .join('<span class="freeze-connector" aria-hidden="true"></span>');
  return `<div class="freeze-timeline" aria-label="四阶段预测冻结状态">
    <div class="freeze-timeline-head"><span>预测冻结</span><span>${escapeHtml(prediction.freeze_stage ?? "—")}</span></div>
    <div class="freeze-stages">${stages}</div>
  </div>`;
}

function probabilityPercent(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) && numeric >= 0 && numeric <= 1 ? `${(numeric * 100).toFixed(1)}%` : "—";
}

function numericValue(value, digits = 2) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toFixed(digits) : "—";
}

function predictionForMatch(match) {
  const locked = state.predictions?.predictions?.find((item) => item.fixture_id === match.id);
  const research = state.predictions?.research_predictions?.find((item) => item.fixture_id === match.id);
  return {
    prediction: locked || research || null,
    researchOnly: !locked && Boolean(research),
  };
}

function sortedProbabilityEntries(value, limit = Infinity) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  return Object.entries(value)
    .filter(([, probability]) => Number.isFinite(Number(probability)) && Number(probability) >= 0)
    .sort((left, right) => Number(right[1]) - Number(left[1]))
    .slice(0, limit);
}

function predictionEvidenceBlock(prediction) {
  if (!prediction) return "";
  const sources = Array.isArray(prediction.live_feature_sources)
    ? prediction.live_feature_sources.filter((item) => item && typeof item === "object")
    : [];
  const trace = prediction.factor_trace && typeof prediction.factor_trace === "object" ? prediction.factor_trace : {};
  const appliedSteps = Array.isArray(trace.steps) ? trace.steps : [];
  const notAppliedSteps = Array.isArray(trace.not_applied) ? trace.not_applied : [];
  const featureTimes = Array.isArray(prediction.feature_times) ? prediction.feature_times.filter(Boolean) : [];
  if (!sources.length && !appliedSteps.length && !notAppliedSteps.length && !featureTimes.length) return "";

  const sourceRows = sources.slice(0, 12).map((item) => {
    const hash = item.raw_sha256 || item.content_sha256 || item.wire_sha256;
    const hashLabel = typeof hash === "string" && hash.length >= 12 ? ` · ${escapeHtml(hash.slice(0, 12))}…` : "";
    return `<span class="prediction-evidence-row">
      <b>${escapeHtml(item.field || "未命名字段")}</b>
      <span>${escapeHtml(item.name || "来源待确认")} · ${escapeHtml(formatDateTime(item.observed_at || item.source_retrieved_at))}${hashLabel}</span>
    </span>`;
  }).join("");

  const deltaText = (delta) => {
    if (!delta || typeof delta !== "object") return "";
    return Object.entries(delta)
      .filter(([, value]) => Number.isFinite(Number(value)))
      .map(([key, value]) => `${key} ${Number(value) >= 0 ? "+" : ""}${(Number(value) * 100).toFixed(1)}pp`)
      .join(" · ");
  };
  const traceRows = [...appliedSteps.map((item) => ({ ...item, traceStatus: "已入模" })), ...notAppliedSteps.map((item) => ({ ...item, traceStatus: item.status === "missing" ? "缺失" : "已抓取·未入模" }))]
    .filter((item) => item && typeof item === "object")
    .slice(0, 12)
    .map((item) => `<span class="prediction-evidence-row prediction-evidence-row--${item.traceStatus === "已入模" ? "used" : "held"}">
      <b>${escapeHtml(item.label || item.id || "模型步骤")}</b>
      <span>${escapeHtml(item.traceStatus)}${item.source_name ? ` · ${escapeHtml(item.source_name)}` : ""}${item.observed_at ? ` · ${escapeHtml(formatDateTime(item.observed_at))}` : ""}${deltaText(item.delta_probability_1x2) ? ` · ${escapeHtml(deltaText(item.delta_probability_1x2))}` : ""}</span>
    </span>`)
    .join("");

  const meta = [
    `冻结截止 ${formatDateTime(prediction.freeze_cutoff_at)}`,
    `观察 ${formatDateTime(prediction.prediction_observed_at || prediction.as_of)}`,
    prediction.model_training_cutoff ? `训练截止 ${formatDateTime(prediction.model_training_cutoff)}` : null,
    featureTimes.length ? `特征时间 ${featureTimes.length} 条` : null,
  ].filter(Boolean).join(" · ");
  return `<details class="prediction-evidence">
    <summary>证据与模型步骤 <span>${escapeHtml(meta)}</span></summary>
    <div class="prediction-evidence-meta">${escapeHtml(meta)}</div>
    <div class="prediction-evidence-columns">
      ${sourceRows ? `<div><div class="prediction-evidence-title">来源观察与哈希</div><div class="prediction-evidence-list">${sourceRows}</div></div>` : ""}
      ${traceRows ? `<div><div class="prediction-evidence-title">模型步骤与入模状态</div><div class="prediction-evidence-list">${traceRows}</div></div>` : ""}
    </div>
    ${trace.note ? `<small class="prediction-evidence-note">${escapeHtml(trace.note)}</small>` : ""}
  </details>`;
}

function predictionDetailBlock(prediction) {
  if (!prediction) return "";
  const totalGoals = sortedProbabilityEntries(prediction.total_goals_probability);
  const scorelines = Array.isArray(prediction.scoreline_top5)
    ? prediction.scoreline_top5.filter(
        (item) => item && typeof item.score === "string" && Number.isFinite(Number(item.probability)),
      )
    : [];
  const halfFull = sortedProbabilityEntries(prediction.half_full_probability);
  const handicap = sortedProbabilityEntries(prediction.handicap_probability);
  const totalOverUnder = sortedProbabilityEntries(prediction.total_over_under_probability);

  const valueList = (entries, labelMap = {}) =>
    entries
      .map(
        ([label, probability]) =>
          `<span class="prediction-detail-item"><span>${escapeHtml(labelMap[label] || label)}</span><b>${probabilityPercent(probability)}</b></span>`,
      )
      .join("");
  const scorelineList = scorelines
    .map(
      (item) =>
        `<span class="prediction-detail-item"><span>${escapeHtml(item.score)}</span><b>${probabilityPercent(item.probability)}</b></span>`,
    )
    .join("");
  const scorelineCoverage = scorelines.reduce((sum, item) => sum + Number(item.probability), 0);
  const halfFullLabels = { H: "主", D: "平", A: "客" };
  const halfFullLabel = (label) =>
    String(label)
      .split("/")
      .map((part) => halfFullLabels[part] || part)
      .join("/");
  const handicapLabels = {
    win: "赢",
    push: "走",
    loss: "输",
    half_win: "半赢",
    half_loss: "半输",
  };
  const totalLine = Number.isFinite(Number(prediction.total_over_under_line))
    ? ` ${Number(prediction.total_over_under_line).toFixed(1)}`
    : " · 结算线未记录";
  const market = prediction.market_probability;
  const marketEntries = sortedProbabilityEntries(market, 3);
  const marketLabels = { home: "主", draw: "平", away: "客" };
  const marketSummary = marketEntries.length
    ? `<div class="prediction-detail prediction-detail--market"><div class="prediction-detail-title">市场对照</div><div class="prediction-detail-values">${valueList(marketEntries, marketLabels)}</div></div>`
    : "";

  const blocks = [];
  if (scorelineList) {
    blocks.push(
      `<div class="prediction-detail prediction-detail--scoreline"><div class="prediction-detail-title">比分 Top ${scorelines.length}</div><div class="prediction-detail-values">${scorelineList}</div><small>Top ${scorelines.length} 累计覆盖 ${probabilityPercent(scorelineCoverage)} · 完整矩阵已归档</small></div>`,
    );
  }
  if (totalGoals.length) {
    blocks.push(
      `<div class="prediction-detail"><div class="prediction-detail-title">总进球分布</div><div class="prediction-detail-values">${valueList(totalGoals)}</div></div>`,
    );
  }
  if (halfFull.length) {
    blocks.push(
      `<div class="prediction-detail"><div class="prediction-detail-title">半全场联合</div><div class="prediction-detail-values">${valueList(halfFull, Object.fromEntries(halfFull.map(([label]) => [label, halfFullLabel(label)])))}</div></div>`,
    );
  }
  if (handicap.length) {
    const line = Number.isFinite(Number(prediction.handicap_line)) ? ` ${Number(prediction.handicap_line).toFixed(1)}` : "";
    blocks.push(
      `<div class="prediction-detail"><div class="prediction-detail-title">让球${line}</div><div class="prediction-detail-values">${valueList(handicap, handicapLabels)}</div></div>`,
    );
  }
  if (totalOverUnder.length) {
    blocks.push(
      `<div class="prediction-detail"><div class="prediction-detail-title">大小球${escapeHtml(totalLine)}</div><div class="prediction-detail-values">${valueList(totalOverUnder, { over: "大", push: "走", under: "小", half_win: "半赢", half_loss: "半输" })}</div></div>`,
    );
  }
  if (!blocks.length && !marketSummary) return "";
  return `<div class="prediction-details" aria-label="赛前预测分布明细">${blocks.join("")}${marketSummary}${predictionEvidenceBlock(prediction)}</div>`;
}

function drawerSourceMarkup(label, source) {
  if (!source || typeof source !== "object") {
    return `<div class="match-drawer-source"><strong>${escapeHtml(label)}</strong><span>暂无可验证观测，不补零。</span></div>`;
  }
  const name = source.name || source.provider || source.sourceName || "来源待确认";
  const observed = source.retrieved_at || source.observed_at || source.effective_at || source.retrievedAt || source.observedAt;
  const hash = source.raw_sha256 || source.content_sha256 || source.wire_sha256 || source.rawHash || source.contentSha256;
  const candidateUrl = source.url || source.sourceUrl;
  const url = typeof candidateUrl === "string" && candidateUrl.startsWith("https://") ? candidateUrl : null;
  const details = [
    escapeHtml(name),
    observed ? `观测 ${escapeHtml(formatDateTime(observed))}` : "观测时间缺失",
    typeof hash === "string" && hash.length >= 12 ? `SHA ${escapeHtml(hash.slice(0, 12))}…` : "哈希缺失",
  ].join(" · ");
  return `<div class="match-drawer-source">
    <strong>${escapeHtml(label)}</strong>
    <span>${details}${url ? ` · <a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">打开来源</a>` : ""}</span>
  </div>`;
}

function liveEvidenceForMatch(match) {
  const compact = state.snapshot?.fixture_evidence?.[match.id];
  if (compact && typeof compact === "object") {
    const hasEvents = Array.isArray(compact.events) && compact.events.length > 0;
    const hasStats = Array.isArray(compact.matchStats) && compact.matchStats.length > 0;
    if (hasEvents || hasStats) return compact;
  }

  // The online API exposes the bounded match-center projection rather than
  // the offline bundle's per-fixture evidence map. Normalize that raw
  // provider shape into the same display contract without promoting it to a
  // pre-match feature or inventing source hashes that were not observed.
  const center = state.snapshot?.current_data?.match_center;
  const rows = [
    ...(Array.isArray(center?.live) ? center.live : []),
    ...(Array.isArray(center?.recent_finished) ? center.recent_finished : []),
  ];
  const row = rows.find((item) => item && item.fixture_id === match.id);
  if (!row || typeof row !== "object") return null;
  const source = row.source && typeof row.source === "object"
    ? row.source
    : {
        name: center?.provider || "ESPN event summary",
        retrieved_at: row.observed_at || center?.retrieved_at,
      };
  const rawEvents = Array.isArray(row.incidents?.events) ? row.incidents.events : [];
  const events = rawEvents
    .filter((item) => item && typeof item === "object")
    .map((event) => ({
      ...event,
      detail: event.detail || event.text || event.type || "比赛事件",
      label: event.label || event.type,
      player: event.player || event.participants?.[0],
      assist: event.assist || event.participants?.[1],
      observedAt: event.observedAt || row.observed_at || center?.retrieved_at,
      sourceName: source.name || source.provider,
      retrievedAt: source.retrieved_at || source.observed_at,
    }));
  const stats = row.match_stats && typeof row.match_stats === "object"
    ? [{ ...row.match_stats, sourceName: source.name || source.provider, retrievedAt: source.retrieved_at || source.observed_at }]
    : [];
  return { events, matchStats: stats, source };
}

function drawerLiveEvidenceMarkup(match) {
  const evidence = liveEvidenceForMatch(match);
  if (!evidence || typeof evidence !== "object") return "";
  const events = Array.isArray(evidence.events) ? evidence.events.filter((item) => item && typeof item === "object") : [];
  const stats = Array.isArray(evidence.matchStats) ? evidence.matchStats.filter((item) => item && typeof item === "object") : [];
  if (!events.length && !stats.length) return "";

  const eventRows = events
    .slice(-16)
    .reverse()
    .map((event) => {
      const minute = event.minute ? `${escapeHtml(event.minute)} · ` : "";
      const people = [event.player, event.assist].filter(Boolean).map(escapeHtml).join(" · ");
      const detail = event.detail || event.label || "比赛事件";
      const score = event.score && Number.isInteger(event.score.home) && Number.isInteger(event.score.away)
        ? ` · ${event.score.home}—${event.score.away}`
        : "";
      const observed = event.wallclock || event.occurredAt || event.observedAt;
      return `<div class="match-drawer-event">
        <span class="match-drawer-event-minute">${minute}${escapeHtml(formatDateTime(observed))}</span>
        <span class="match-drawer-event-text"><strong>${escapeHtml(detail)}</strong>${people ? `<small>${people}</small>` : ""}</span>
        <span class="match-drawer-event-score">${escapeHtml(score ? score.slice(3) : "")}</span>
      </div>`;
    })
    .join("");

  const statRows = [];
  const teams = stats[0]?.teams && typeof stats[0].teams === "object" ? stats[0].teams : {};
  const homeStats = Array.isArray(teams.home?.statistics) ? teams.home.statistics : [];
  const awayStats = Array.isArray(teams.away?.statistics) ? teams.away.statistics : [];
  const byName = (items) => Object.fromEntries(items.map((item) => [item.name, item]));
  const homeByName = byName(homeStats);
  const awayByName = byName(awayStats);
  ["possessionPct", "totalShots", "shotsOnTarget", "wonCorners", "foulsCommitted", "offsides"].forEach((name) => {
    const home = homeByName[name];
    const away = awayByName[name];
    if (!home && !away) return;
    statRows.push(`<span class="match-drawer-stat"><span>${escapeHtml(home?.label || away?.label || name)}</span><strong>${escapeHtml(home?.display_value ?? "—")} · ${escapeHtml(away?.display_value ?? "—")}</strong></span>`);
  });

  const source = evidence.source || events[0] || stats[0];
  const stateLabel = match.status === "live" ? "实时观测" : "赛后审计";
  return `<section class="match-drawer-section match-drawer-live-evidence">
    <div class="match-drawer-section-head"><h3>赛中事件与统计</h3><span>${stateLabel} · 仅展示</span></div>
    <div class="match-drawer-note"><strong>时间边界</strong>：这些记录发生在开球后，只用于实时/赛后核查，不回填任何赛前冻结特征或预测。</div>
    ${statRows.length ? `<div class="match-drawer-grid">${statRows.join("")}</div>` : ""}
    ${eventRows ? `<div class="match-drawer-event-list">${eventRows}</div>` : '<div class="match-drawer-note">暂无事件明细。</div>'}
    ${drawerSourceMarkup("赛中来源", source)}
  </section>`;
}

function drawerProbabilityMarkup(probability, labels = {}) {
  return sortedProbabilityEntries(probability, 6)
    .map(([key, value]) => `<span class="match-drawer-stat"><span>${escapeHtml(labels[key] || key)}</span><strong>${probabilityPercent(value)}</strong></span>`)
    .join("");
}

function drawerFormMarkup(side, feature) {
  if (!feature || typeof feature !== "object") {
    return `<div class="match-drawer-note"><strong>${escapeHtml(side)}近期状态</strong>：没有达到可展示的样本，不把缺失当作 0。</div>`;
  }
  const source = feature.source || {};
  const stats = [
    ["xG 进", numericValue(feature.xg_for)],
    ["xG 失", numericValue(feature.xg_against)],
    ["实际进球", numericValue(feature.goals_for, 1)],
    ["实际失球", numericValue(feature.goals_against, 1)],
    ["样本", feature.sample_n == null ? "—" : formatNumber(feature.sample_n)],
  ];
  return `<div class="match-drawer-section">
    <div class="match-drawer-section-head"><h3>${escapeHtml(side)}近期状态</h3><span>${escapeHtml(source.name || "状态源")}</span></div>
    <div class="match-drawer-grid">${stats.map(([label, value]) => `<span class="match-drawer-stat"><span>${label}</span><strong>${escapeHtml(value)}</strong></span>`).join("")}</div>
    <div class="match-drawer-note">数据截至 ${escapeHtml(formatDateTime(feature.last_match_at))} · 近 ${escapeHtml(feature.sample_n ?? "—")} 场 · 不等同于本场确认首发。</div>
  </div>`;
}

function drawerLineupMarkup(features) {
  const official = features.official_lineup && typeof features.official_lineup === "object" ? features.official_lineup : null;
  const lineup = official?.lineups && typeof official.lineups === "object" ? official.lineups : null;
  const teamStatus = features.team_status && typeof features.team_status === "object" ? features.team_status : null;
  const statusLabel = lineup?.confirmed
    ? "官方首发已确认"
    : lineup?.available
      ? "页面已观测 · 尚未满足完整首发门禁"
      : teamStatus?.confirmed
        ? "提供方已标记确认 · 待完整性核验"
        : "尚未发布或未满足确认条件";
  const sideNames = (side) => {
    const players = Array.isArray(lineup?.[side]?.players) ? lineup[side].players : [];
    const names = players
      .map((player) => player?.name || player?.display_name || player?.short_name)
      .filter(Boolean)
      .slice(0, 11);
    return names.length ? names.map(escapeHtml).join(" · ") : "暂无公开首发名单";
  };
  const injuries = official?.injuries && typeof official.injuries === "object" ? official.injuries : null;
  const injuryCount = injuries
    ? (Array.isArray(injuries.home) ? injuries.home.length : 0) + (Array.isArray(injuries.away) ? injuries.away.length : 0)
    : 0;
  const note = official?.lineups?.missing_fields?.length
    ? `缺失字段：${official.lineups.missing_fields.join("、")}`
    : teamStatus?.note || "普通球队名单不会自动当作首发、伤停或预计分钟。";
  return `<div class="match-drawer-section">
    <div class="match-drawer-section-head"><h3>首发与可用性</h3><span>${escapeHtml(statusLabel)}</span></div>
    <div class="match-drawer-grid">
      <span class="match-drawer-stat"><span>主队首发</span><strong>${sideNames("home")}</strong></span>
      <span class="match-drawer-stat"><span>客队首发</span><strong>${sideNames("away")}</strong></span>
      <span class="match-drawer-stat"><span>模型准入</span><strong>${official?.lineups?.model_eligible === true || teamStatus?.model_eligible === true ? "已满足" : "未满足"}</strong></span>
      <span class="match-drawer-stat"><span>伤停记录</span><strong>${injuryCount ? `${formatNumber(injuryCount)} 条` : "暂无可验证记录"}</strong></span>
    </div>
    <div class="match-drawer-note"><strong>门禁说明</strong>：${escapeHtml(note)}${official?.source?.time_basis ? ` · 时间依据 ${escapeHtml(official.source.time_basis)}` : ""}</div>
    ${drawerSourceMarkup("官方/首发", official?.source || teamStatus?.source)}
  </div>`;
}

function drawerContextMarkup(features) {
  const weather = features.weather && typeof features.weather === "object" ? features.weather : null;
  const market = features.market && typeof features.market === "object" ? features.market : null;
  const lottery = features.lottery_market && typeof features.lottery_market === "object" ? features.lottery_market : null;
  const weatherBlock = weather
    ? `<div class="match-drawer-section"><div class="match-drawer-section-head"><h3>天气与场地环境</h3><span>${escapeHtml(weather.source?.name || "Open-Meteo")}</span></div><div class="match-drawer-grid">
        <span class="match-drawer-stat"><span>温度</span><strong>${escapeHtml(numericValue(weather.temperature_c, 1))} °C</strong></span>
        <span class="match-drawer-stat"><span>湿度</span><strong>${escapeHtml(numericValue(weather.humidity_percent, 0))}%</strong></span>
        <span class="match-drawer-stat"><span>降水概率</span><strong>${escapeHtml(numericValue(weather.precipitation_probability_percent, 0))}%</strong></span>
        <span class="match-drawer-stat"><span>风速</span><strong>${escapeHtml(numericValue(weather.wind_speed_kmh, 1))} km/h</strong></span>
      </div>${drawerSourceMarkup("天气观测", weather.source)}</div>`
    : `<div class="match-drawer-section"><div class="match-drawer-section-head"><h3>天气与场地环境</h3><span>不可用</span></div><div class="match-drawer-note">当前没有覆盖本场开球时段的天气预报；不补写默认天气。</div></div>`;
  const marketBlock = market?.probability && Object.keys(market.probability).length
    ? `<div class="match-drawer-section"><div class="match-drawer-section-head"><h3>当前市场对照</h3><span>${escapeHtml(market.provider || "公开市场")}</span></div><div class="match-drawer-grid">${drawerProbabilityMarkup(market.probability, { home: "主胜", draw: "平局", away: "客胜" })}</div>${drawerSourceMarkup("市场快照", market.source)}</div>`
    : `<div class="match-drawer-section"><div class="match-drawer-section-head"><h3>当前市场对照</h3><span>不可用</span></div><div class="match-drawer-note">暂无可验证的当前市场概率。历史赔率不会伪装成临场市场。</div></div>`;
  const lotteryBlock = lottery?.hhad_probability && Object.keys(lottery.hhad_probability).length
    ? `<div class="match-drawer-section"><div class="match-drawer-section-head"><h3>体彩公开口径</h3><span>仅购买场景对照 · ${escapeHtml(lottery.hhad_line || "让球线未记录")}</span></div><div class="match-drawer-grid">${drawerProbabilityMarkup(lottery.hhad_probability, { h: "让胜", d: "让平", a: "让负" })}</div><div class="match-drawer-note">这是体彩公开赛程/赔率快照，按官方结算口径单独记录；不等于模型优势，也不输出投注金额或保证性指令。</div>${drawerSourceMarkup("体彩快照", lottery.source)}</div>`
    : "";
  return `${weatherBlock}${marketBlock}${lotteryBlock}`;
}

function renderMatchDrawer(match) {
  const drawer = $("#match-drawer");
  const target = $("#match-drawer-content");
  if (!drawer || !target || !match) return;
  const league = leagueById(match.competition_id);
  const features = match.current_features && typeof match.current_features === "object" ? match.current_features : {};
  const { prediction, researchOnly } = predictionForMatch(match);
  const score = match.score && typeof match.score.home === "number" && typeof match.score.away === "number"
    ? `${match.score.home} — ${match.score.away}`
    : "—";
  const status = STATUS_LABELS[match.status] || match.status || "未知状态";
  const coverage = prediction?.coverage || {};
  const lowCoverage = prediction && (coverage.level === "low" || coverage.critical_conflict === true);
  const blocked = state.predictions?.blocked?.find((item) => item.fixture_id === match.id);
  const predictionBlock = prediction
    ? lowCoverage
      ? `<div class="match-drawer-note"><strong>预测已隔离</strong>：${researchOnly ? "研究草稿 · " : ""}覆盖等级 ${escapeHtml(coverage.level || "冲突")}${Array.isArray(coverage.missing) && coverage.missing.length ? ` · 缺少 ${escapeHtml(coverage.missing.join("、"))}` : ""}。概率不在这里展示。</div>${freezeTimeline(prediction)}${predictionEvidenceBlock(prediction)}`
      : predictionDetailBlock(prediction)
    : `<div class="match-drawer-note"><strong>没有合法冻结预测</strong>：${escapeHtml(blocked?.reason || (match.status === "upcoming" ? "等待合法冻结截点或必要数据" : "该场没有归档预测"))}。</div>`;
  const sources = [
    ["赛程与身份", match.source],
    ["主队近况", features.home?.source],
    ["客队近况", features.away?.source],
    ["当前市场", features.market?.source],
    ["体彩快照", features.lottery_market?.source],
    ["天气", features.weather?.source],
    ["官方首发", features.official_lineup?.source],
    ["公开页面线索", features.whoscored_market?.source],
  ];
  target.innerHTML = `
    <div class="match-drawer-kicker"><span>${escapeHtml(league?.name_zh || match.competition_id)}</span><span>${escapeHtml(status)}</span><span>${escapeHtml(formatDateTime(match.kickoff_at))} 开赛</span></div>
    <div class="match-drawer-scoreboard">
      <div class="match-drawer-team"><strong>${escapeHtml(match.home_team)}</strong><small>${escapeHtml(features.home?.provider_team_id ? `provider ${features.home.provider_team_id}` : "主队身份已登记")}</small></div>
      <div class="match-drawer-score"><strong>${escapeHtml(score)}</strong><small>${escapeHtml(match.status === "upcoming" ? "未赛" : "90 分钟口径")}</small></div>
      <div class="match-drawer-team"><strong>${escapeHtml(match.away_team)}</strong><small>${escapeHtml(features.away?.provider_team_id ? `provider ${features.away.provider_team_id}` : "客队身份已登记")}</small></div>
    </div>
    <section class="match-drawer-section">
      <div class="match-drawer-section-head"><h3>本场结论状态</h3><span>${prediction ? (researchOnly ? "研究草稿" : "冻结记录") : "暂无合法预测"}</span></div>
      <div class="match-drawer-note"><strong>${prediction ? `覆盖等级 ${escapeHtml(coverage.level || "未声明")}` : "数据不足"}</strong> · 本面板只呈现截至已观测时间的证据；缺失、冲突和未确认信息不会被补全。</div>
      ${predictionBlock}
    </section>
    ${drawerFormMarkup("主队", features.home)}
    ${drawerFormMarkup("客队", features.away)}
    ${drawerLineupMarkup(features)}
    ${drawerContextMarkup(features)}
    ${drawerLiveEvidenceMarkup(match)}
    <section class="match-drawer-section">
      <div class="match-drawer-section-head"><h3>来源与时间账本</h3><span>原始哈希可追溯</span></div>
      <div class="match-drawer-source-list">${sources.map(([label, source]) => drawerSourceMarkup(label, source)).join("")}</div>
    </section>`;
}

let drawerReturnFocus = null;

function closeMatchDrawer() {
  const drawer = $("#match-drawer");
  const scrim = $("#match-drawer-scrim");
  if (!drawer || !scrim) return;
  drawer.hidden = true;
  scrim.hidden = true;
  drawer.setAttribute("aria-hidden", "true");
  document.body.classList.remove("drawer-open");
  state.activeMatchId = null;
  if (drawerReturnFocus && typeof drawerReturnFocus.focus === "function") drawerReturnFocus.focus();
  drawerReturnFocus = null;
}

function openMatchDrawer(matchId, sourceElement = null) {
  const match = state.snapshot?.matches?.find((item) => item.id === matchId);
  const drawer = $("#match-drawer");
  const scrim = $("#match-drawer-scrim");
  if (!match || !drawer || !scrim) return;
  state.activeMatchId = matchId;
  drawerReturnFocus = sourceElement || document.activeElement;
  renderMatchDrawer(match);
  drawer.hidden = false;
  scrim.hidden = false;
  drawer.setAttribute("aria-hidden", "false");
  document.body.classList.add("drawer-open");
  requestAnimationFrame(() => $("#match-drawer-close")?.focus());
}

function probabilityBlock(match) {
  const lockedPrediction = state.predictions?.predictions?.find((item) => item.fixture_id === match.id);
  const researchPrediction = state.predictions?.research_predictions?.find((item) => item.fixture_id === match.id);
  // The offline bundle contains a separate research-only projection. It may
  // be shown for researchers, but it must never replace a current-lock row or
  // turn a low-coverage locked row into a stronger conclusion.
  const prediction = lockedPrediction || researchPrediction;
  const researchOnly = !lockedPrediction && Boolean(researchPrediction);
  const currentMarket = (match.current_features || {}).market;

  // A low-coverage prediction is still useful for audit and future scoring,
  // but the product contract says it must not be rendered as a strong
  // conclusion. Keep the freeze timeline visible so the missing stage is
  // auditable, while withholding the probability bar itself.
  if (prediction) {
    const coverage = prediction.coverage || {};
    const level = coverage.level || "low";
    const missing = Array.isArray(coverage.missing) ? coverage.missing : [];
    if (level === "low" || coverage.critical_conflict === true) {
      const missingText = missing.length ? ` · 缺少 ${missing.join("、")}` : "";
      return `<div class="odds-empty odds-empty--blocked">
        <strong>预测不展示</strong>
        <span>${researchOnly ? "研究草稿 · " : ""}覆盖等级：${escapeHtml(level === "low" ? "低" : "冲突隔离")}${escapeHtml(missingText)}</span>
      </div>${freezeTimeline(prediction)}${predictionEvidenceBlock(prediction)}`;
    }
  }

  let probabilities = null;
  let title = "";
  let stamp = "";

  if (prediction?.dixon_coles_probability || prediction?.primary_probability_1x2) {
    probabilities = prediction.dixon_coles_probability || prediction.primary_probability_1x2;
    const level = prediction.coverage?.level || "medium";
    title = `${researchOnly ? "研究草稿" : "模型"} · research_only · 覆盖${level === "high" ? "高" : "中"}`;
    stamp = `截点 ${formatDateTime(prediction.as_of)}`;
  } else if (currentMarket?.probability) {
    probabilities = currentMarket.probability;
    title = `当前市场 · ${currentMarket.provider ?? "未知"}`;
    stamp = `采集 ${formatDateTime(currentMarket.retrieved_at)}`;
  } else if (match.market_probability) {
    probabilities = match.market_probability;
    title = "历史去水市场";
    stamp = "随结果一同归档";
  }

  if (!probabilities) {
    const blockedReason = state.predictions?.blocked?.find((item) => item.fixture_id === match.id)?.reason;
    const reason = blockedReason === "no_causal_freeze_record"
      ? "等待合法冻结截点，未生成即时替代预测"
      : state.blockedFixtures.has(match.id)
      ? "样本不足，预测已阻断"
      : match.status === "upcoming"
        ? "暂无可验证的市场快照"
        : "该场没有归档赔率";
    return `<div class="odds-empty">${reason}</div>`;
  }

  const score = match.score;
  const outcome =
    score && typeof score.home === "number" && typeof score.away === "number"
      ? score.home > score.away
        ? "home"
        : score.away > score.home
          ? "away"
          : "draw"
      : null;

  const legs = [
    ["主", probabilities.home, "home"],
    ["平", probabilities.draw, "draw"],
    ["客", probabilities.away, "away"],
  ];

  return `
    <div class="odds-head"><span>${escapeHtml(title)}</span><span>${escapeHtml(stamp)}</span></div>
    <div class="odds-bar" aria-hidden="true">
      ${legs
        .map(([, value, key]) => `<span class="odds-seg odds-seg--${key}" style="width:${(value * 100).toFixed(1)}%"></span>`)
        .join("")}
    </div>
    <div class="odds-labels">
      ${legs
        .map(
          ([label, value, key]) =>
            `<span><i class="pip pip--${key}" aria-hidden="true"></i>${label}<b>${(value * 100).toFixed(0)}%</b>${
              outcome === key ? '<span class="hit" title="实际结果">✓</span>' : ""
            }</span>`,
        )
        .join("")}
    </div>
    ${
      prediction?.coverage?.level === "medium"
        ? `<div class="odds-risk">覆盖等级：中 · 仅作研究对照${
            prediction.coverage.missing?.length
              ? ` · 缺少 ${escapeHtml(prediction.coverage.missing.join("、"))}`
              : ""
          }</div>`
        : ""
    }
    ${predictionDetailBlock(prediction)}
    ${freezeTimeline(prediction)}
  `;
}

function renderMatches() {
  const matches = filteredMatches();
  const visible = matches.slice(0, state.visible);
  $("#fixture-count").textContent = `${formatNumber(matches.length)} 场符合筛选`;

  $("#fixture-list").innerHTML = visible.length
    ? visible
        .map((match) => {
          const league = leagueById(match.competition_id);
          const score = match.score ?? { home: "—", away: "—" };
          const settled = typeof score.home === "number" && typeof score.away === "number";
          const homeLost = settled && score.home < score.away;
          const awayLost = settled && score.away < score.home;

          const tag =
            match.status === "live"
              ? '<span class="tag tag--live">进行中</span>'
              : match.status === "upcoming"
                ? `<span class="tag tag--upcoming">${escapeHtml(timeUntil(match.kickoff_at))}</span>`
                : `<span class="tag">${escapeHtml(STATUS_LABELS[match.status] ?? match.status)}</span>`;

          const features = match.current_features || {};
          const formLine = (side) => {
            const data = features[side];
            if (!data || typeof data.xg_for !== "number") return "";
            return `<span>${escapeHtml(data.team)} 近 ${data.sample_n} 场 xG ${data.xg_for.toFixed(2)} · 失 ${data.xg_against.toFixed(2)}</span>`;
          };
          const form = [formLine("home"), formLine("away")].filter(Boolean).join("");

          const source = match.source || {};
          const provenance =
            source.file
              ? `${escapeHtml(source.name)} · ${escapeHtml(source.file)}`
              : `${escapeHtml(source.name ?? "未知来源")} · 采集 ${formatDateTime(source.retrieved_at)}`;

          return `
            <article class="fixture" aria-label="${escapeHtml(match.home_team)} 对 ${escapeHtml(match.away_team)}">
              <div class="fixture-when">
                <time class="fixture-date" datetime="${escapeHtml(match.kickoff_at)}">${formatDate(match.kickoff_at)}</time>
                <span class="fixture-kick">${formatTime(match.kickoff_at)} 开赛</span>
                ${tag}
              </div>
              <div>
                <div class="side ${homeLost ? "side--lost" : ""}">
                  <span class="side-name">${escapeHtml(match.home_team)}</span>
                  <span class="side-score">${escapeHtml(score.home)}</span>
                </div>
                <div class="side ${awayLost ? "side--lost" : ""}">
                  <span class="side-name">${escapeHtml(match.away_team)}</span>
                  <span class="side-score">${escapeHtml(score.away)}</span>
                </div>
                ${form ? `<div class="xg-row">${form}</div>` : ""}
                <div class="fixture-meta">${escapeHtml(league?.name_zh ?? match.competition_id)} ${escapeHtml(match.season)} · ${provenance}</div>
                <button class="fixture-open" type="button" data-open-match="${escapeHtml(match.id)}" aria-label="打开 ${escapeHtml(match.home_team)} 对 ${escapeHtml(match.away_team)} 的单场分析">打开单场分析</button>
              </div>
              <div class="fixture-odds">${probabilityBlock(match)}</div>
            </article>
          `;
        })
        .join("")
    : `<div class="empty">
         <strong>没有符合条件的比赛</strong>
         <p>更换联赛、赛季或球队关键词，或清除筛选。</p>
       </div>`;

  $("#load-more").hidden = visible.length >= matches.length;
  $("#fixture-shown").textContent = matches.length
    ? `显示 ${formatNumber(visible.length)} / ${formatNumber(matches.length)} 场`
    : "无匹配结果";
}

/* ----------------------------------------------------------- source health */

function renderSources() {
  $("#source-list").innerHTML = state.snapshot.competitions
    .map((league) => {
      const status = league.current_source_status ?? league.source_status;
      const dot =
        status === "fresh" ? "good" : status === "stale" ? "serious" : status === "unavailable" ? "critical" : "warning";
      const quality = league.data_quality || {};
      const scores = Math.round((quality.score_completeness ?? 0) * 100);
      const odds = Math.round((quality.odds_completeness ?? 0) * 100);
      // Only call out completeness that is actually short — a row of full
      // bars would carry no information.
      const gaps = [
        scores < 100 ? `比分 ${scores}%` : null,
        odds < 100 ? `赔率 ${odds}%` : null,
      ].filter(Boolean);
      return `
        <div class="source-row">
          <strong>${escapeHtml(league.name_zh)}</strong>
          <span class="source-status">
            <span class="dot dot--${dot}" aria-hidden="true"></span>
            ${escapeHtml(SOURCE_STATUS_LABELS[status] ?? status)}
          </span>
          <span class="source-meta">当前 ${formatNumber(league.current_fixture_count ?? 0)} 场 · xG ${formatNumber(league.current_xg_team_count ?? 0)} 队 · 历史 ${formatNumber(quality.row_count ?? 0)} 行</span>
          <span class="source-meta">${
            gaps.length
              ? `<span class="gap-flag">缺口 ${escapeHtml(gaps.join(" · "))}</span>`
              : "比分与赔率完整"
          }</span>
        </div>`;
    })
    .join("");

  const registry = state.snapshot.current_data?.source_registry || [];
  // The same predicate drives both the summary and the detailed rows.  Keep
  // it at renderSources scope: the detail list must still render when the
  // summary mount point is absent in a reduced/offline page fixture.
  const isExecutionLayer = (row) => row?.role === "fetch_runtime" || row?.fact_source === false;
  const registrySummary = $("#source-registry-summary");
  if (registrySummary) {
    const factSourceCount = registry.filter((row) => !isExecutionLayer(row)).length;
    const executionLayerCount = registry.filter(isExecutionLayer).length;
    const freshCount = registry.filter((row) => ["fresh", "ok", "enabled"].includes(row?.runtime?.status || row?.status)).length;
    const blockedCount = registry.filter((row) => ["blocked_by_robots", "forbidden", "quarantined"].includes(row?.runtime?.status || row?.status)).length;
    const crawlRuntime = registry.find((row) => row?.id === "crawl4ai_allowlisted_pages")?.runtime || {};
    const crawlFanout = Number(crawlRuntime.fanout?.declared_source_count ?? 0);
    const crawlPages = Number(crawlRuntime.record_count ?? 0);
    registrySummary.innerHTML = `
      <span class="source-summary-item"><b>${formatNumber(factSourceCount)}</b>事实源</span>
      <span class="source-summary-item"><b>${formatNumber(executionLayerCount)}</b>执行层</span>
      <span class="source-summary-item"><b>${formatNumber(freshCount)}</b>本周期可用</span>
      <span class="source-summary-item source-summary-item--muted"><b>${formatNumber(blockedCount)}</b>阻断/隔离</span>
      <span class="source-summary-note">Crawl4AI 共享执行层已扇出 ${formatNumber(crawlFanout)} 个声明事实源、${formatNumber(crawlPages)} 页；页数不计入事实源分母，每页必须带独立 source_id。</span>`;
  }
  const registryTarget = $("#source-registry-list");
  if (registryTarget) {
    registryTarget.innerHTML = registry.length
      ? registry
          .map((row) => {
            const runtime = row.runtime || {};
            const status = runtime.status || row.status || "unavailable";
            const dot = ["fresh", "enabled", "ok"].includes(status)
              ? "good"
              : ["unavailable", "blocked_by_robots", "forbidden"].includes(status)
                ? "critical"
                : "warning";
            const errorText = runtime.error_count
              ? ` · 错误 ${formatNumber(runtime.error_count)}`
              : "";
            const policy = row.model_policy || "未声明模型职责";
            const pageOrRecordLabel = isExecutionLayer(row) ? "页" : "条";
            const extractedCount = Number(runtime.extracted_record_count);
            const extractedText = Number.isFinite(extractedCount) && extractedCount !== Number(runtime.record_count ?? 0)
              ? ` · 解析 ${formatNumber(extractedCount)} 条`
              : "";
            const executionMeta = isExecutionLayer(row)
              ? `共享执行层 · 扇出 ${formatNumber(runtime.fanout?.declared_source_count ?? 0)} 个事实源 · ${formatNumber(runtime.record_count ?? 0)} 页`
              : "";
            return `<div class="source-row source-registry-row">
              <strong>${escapeHtml(row.name || row.id)}</strong>
              <span class="source-status"><span class="dot dot--${dot}" aria-hidden="true"></span>${escapeHtml(SOURCE_STATUS_LABELS[status] || status)}</span>
              <span class="source-meta">本周期 ${formatNumber(runtime.record_count || 0)} ${pageOrRecordLabel}${extractedText}${errorText}${runtime.retrieved_at ? ` · ${escapeHtml(formatDateTime(runtime.retrieved_at))}` : ""}</span>
              ${executionMeta ? `<span class="source-meta">${escapeHtml(executionMeta)}</span>` : ""}
              <span class="source-meta">${escapeHtml(policy)}</span>
            </div>`;
          })
          .join("")
      : '<div class="empty empty--compact"><strong>来源登记不可用</strong><span>本周期快照没有公开来源运行记录。</span></div>';
  }

  const lotteryRows = state.snapshot.current_data?.lottery_sales_schedule || [];
  const current = state.snapshot.current_data || {};
  const independentStatus = current.oddstorm_status || "unavailable";
  const independentLabel = independentStatus === "fresh"
    ? "独立市场对照可用"
    : independentStatus === "degraded"
      ? "独立市场对照降级"
      : "独立市场对照不可用";
  const independentDot = independentStatus === "fresh" ? "good" : independentStatus === "degraded" ? "warning" : "critical";
  $("#independent-market-status").innerHTML = `
    <div class="source-row">
      <strong>独立亚洲盘对照</strong>
      <span class="source-status"><span class="dot dot--${independentDot}" aria-hidden="true"></span>${independentLabel}</span>
      <span class="source-meta">OddStorm · ${formatNumber(current.oddstorm_line_count ?? 0)} 条盘口线</span>
      <span class="source-meta">仅作公开市场校准与审计；非体彩官方赔率，不具备购买资格，未高置信关联前不入模。</span>
    </div>
    <div class="source-row">
      <strong>ESPN 赛前首发回退</strong>
      <span class="source-status"><span class="dot dot--${(current.espn_confirmed_lineup_count ?? 0) > 0 ? "good" : "warning"}" aria-hidden="true"></span>${formatNumber(current.espn_confirmed_lineup_count ?? 0)} 场已确认</span>
      <span class="source-meta">名单总量 ${formatNumber(current.espn_team_status_count ?? 0)} 场</span>
      <span class="source-meta">仅双方各 11 名 starter 且观测早于开赛才进入首发冻结；普通名单不入模。</span>
    </div>`;
  const laligaStatus = current.laliga_official_status || "unavailable";
  const laligaLineups = current.laliga_official_lineup_count ?? 0;
  const laligaConfirmed = current.laliga_official_confirmed_lineup_count ?? 0;
  const laligaNotPublished = current.laliga_official_not_published_count ?? 0;
  const laligaDot = laligaStatus === "ok" && laligaConfirmed > 0 ? "good" : laligaStatus === "degraded" || laligaStatus === "ok" ? "warning" : laligaStatus === "not_requested" ? "warning" : "critical";
  const laligaLabel = laligaStatus === "ok" && laligaConfirmed > 0 ? "含已确认首发" : laligaStatus === "ok" ? "页面可用 · 首发未发布" : laligaStatus === "degraded" ? "官方页面降级" : laligaStatus === "not_requested" ? "尚未进入 48 小时窗口" : "官方页面不可用";
  $("#independent-market-status").innerHTML += `
    <div class="source-row">
      <strong>LaLiga 官方首发</strong>
      <span class="source-status"><span class="dot dot--${laligaDot}" aria-hidden="true"></span>${laligaLabel} · 观测 ${formatNumber(laligaLineups)} 场</span>
      <span class="source-meta">官方 Match Centre 页面 · 仅记录公开观察时间和原始哈希</span>
      <span class="source-meta">完整首发 ${formatNumber(laligaConfirmed)} 场 · 尚未发布 ${formatNumber(laligaNotPublished)} 场 · 必须严格匹配开球时间与双方球队。</span>
    </div>`;
  const bundesligaStatus = current.bundesliga_official_status || "unavailable";
  const bundesligaLineups = current.bundesliga_official_lineup_count ?? 0;
  const bundesligaDot = bundesligaStatus === "ok" ? "good" : bundesligaStatus === "degraded" ? "warning" : bundesligaStatus === "not_requested" ? "warning" : "critical";
  const bundesligaLabel = bundesligaStatus === "ok" ? "官方页面可用" : bundesligaStatus === "degraded" ? "官方页面降级" : bundesligaStatus === "not_requested" ? "尚未进入 48 小时窗口" : "官方页面不可用";
  $("#independent-market-status").innerHTML += `
    <div class="source-row">
      <strong>Bundesliga 官方首发</strong>
      <span class="source-status"><span class="dot dot--${bundesligaDot}" aria-hidden="true"></span>${bundesligaLabel} · ${formatNumber(bundesligaLineups)} 场</span>
      <span class="source-meta">官方 Match Centre / lineup 页面 · 仅记录公开观察时间和原始哈希</span>
      <span class="source-meta">必须严格匹配开球时间与双方球队；完整首发且赛前观察才允许进入模型。</span>
    </div>`;
  const serieAStatus = current.serie_a_official_status || "unavailable";
  const serieALineups = current.serie_a_official_lineup_count ?? 0;
  const serieADot = serieAStatus === "ok" ? "good" : serieAStatus === "degraded" ? "warning" : serieAStatus === "not_requested" ? "warning" : "critical";
  const serieALabel = serieAStatus === "ok" ? "官方 API 可用" : serieAStatus === "degraded" ? "官方 API 降级" : serieAStatus === "not_requested" ? "尚未进入 48 小时窗口" : "官方 API 不可用";
  $("#independent-market-status").innerHTML += `
    <div class="source-row">
      <strong>意甲官方首发</strong>
      <span class="source-status"><span class="dot dot--${serieADot}" aria-hidden="true"></span>${serieALabel} · ${formatNumber(serieALineups)} 场</span>
      <span class="source-meta">Lega Serie A 公开 SDP API · 保存 header/lineups 原始哈希</span>
      <span class="source-meta">严格匹配官方开球时间与双方球队；API 无可信发布时间时只使用系统观察时间。</span>
    </div>`;
  const ligue1Status = current.ligue1_official_status || "unavailable";
  const ligue1Lineups = current.ligue1_official_lineup_count ?? 0;
  const ligue1Confirmed = current.ligue1_official_confirmed_lineup_count ?? 0;
  const ligue1NotPublished = current.ligue1_official_not_published_count ?? 0;
  const ligue1Dot = ligue1Status === "ok" && ligue1Confirmed > 0 ? "good" : ligue1Status === "degraded" || ligue1Status === "ok" ? "warning" : ligue1Status === "not_requested" ? "warning" : "critical";
  const ligue1Label = ligue1Status === "ok" && ligue1Confirmed > 0 ? "含已确认首发" : ligue1Status === "ok" ? "API 可用 · 首发未发布" : ligue1Status === "degraded" ? "官方 API 降级" : ligue1Status === "not_requested" ? "尚未进入 48 小时窗口" : "官方 API 不可用";
  $("#independent-market-status").innerHTML += `
    <div class="source-row">
      <strong>Ligue 1 官方首发</strong>
      <span class="source-status"><span class="dot dot--${ligue1Dot}" aria-hidden="true"></span>${ligue1Label} · 观测 ${formatNumber(ligue1Lineups)} 场</span>
      <span class="source-meta">Ligue 1 官方公开 Match API · 原始响应哈希与 native match ID 已保存</span>
      <span class="source-meta">完整首发 ${formatNumber(ligue1Confirmed)} 场 · roster-only ${formatNumber(ligue1NotPublished)} 场 · 仅严格匹配开球时间与双方球队。</span>
    </div>`;
  const lotteryLabel = (row) => {
    if (row.link_status === "linked_to_model_fixture") return "已关联模型赛程";
    if (row.link_status === "display_only_low_confidence") return "仅展示 · 未入模";
    return "已隔离 · 未入模";
  };
  $("#lottery-sales-list").innerHTML = lotteryRows.length
    ? lotteryRows
        .map(
          (row) => `
            <div class="source-row lottery-row">
              <strong>${escapeHtml(row.match_num || "体彩场次")} · ${escapeHtml(row.league || "未知赛事")}</strong>
              <span class="source-status">${escapeHtml(lotteryLabel(row))}</span>
              <span class="source-meta">${escapeHtml(row.home_team || "—")} — ${escapeHtml(row.away_team || "—")} · ${formatDateTime(row.kickoff_at)}</span>
              <span class="source-meta">${row.link_reason ? `原因：${escapeHtml(row.link_reason)} · ` : ""}实体匹配置信度 ${Math.round((row.entity_match_confidence || 0) * 100)}% · 仅供赛程核对</span>
            </div>`,
        )
        .join("")
    : '<div class="empty"><strong>暂无体彩公开赛程</strong><span>接口未返回可核对的销售场次。</span></div>';

  const roles = state.snapshot.current_data?.roles || {};
  $("#role-list").innerHTML = [
    ["fixtures_and_results", "赛程结果"],
    ["recent_xg_and_form", "近况 xG"],
    ["historical_training", "历史训练"],
    ["current_market", "当前市场"],
    ["independent_asian_market", "独立亚洲盘"],
    ["injuries_and_lineups", "伤停首发"],
    ["official_premier_league_lineups", "英超官方首发"],
    ["official_laliga_lineups", "LaLiga 官方首发"],
    ["official_bundesliga_lineups", "Bundesliga 官方首发"],
    ["official_ligue1_lineups", "Ligue 1 官方首发"],
  ]
    .map(([key, label]) => {
      const value = roles[key];
      const resolved = Array.isArray(value) ? value.join(" · ") : value;
      const missing = !resolved;
      return `<div class="role">
        <dt>${label}</dt>
        <dd${missing ? ' style="color:var(--text-warning)"' : ""}>${escapeHtml(resolved ?? "未接入")}</dd>
      </div>`;
    })
    .join("");
}

function matchCenterStats(row) {
  const teams = row.match_stats?.teams || {};
  const home = teams.home?.statistics || [];
  const away = teams.away?.statistics || [];
  const byName = (items) => Object.fromEntries(items.map((item) => [item.name, item]));
  const homeByName = byName(home);
  const awayByName = byName(away);
  const names = ["possessionPct", "totalShots", "shotsOnTarget", "wonCorners", "foulsCommitted"];
  const rows = names
    .filter((name) => homeByName[name] || awayByName[name])
    .map((name) => {
      const left = homeByName[name]?.display_value ?? "—";
      const right = awayByName[name]?.display_value ?? "—";
      const label = homeByName[name]?.label || awayByName[name]?.label || name;
      return `<span class="match-stat"><b>${escapeHtml(left)}</b><em>${escapeHtml(label)}</em><b>${escapeHtml(right)}</b></span>`;
    });
  return rows.length ? `<div class="match-stats">${rows.join("")}</div>` : "";
}

function matchCenterIncidents(row) {
  const events = Array.isArray(row.incidents?.events) ? row.incidents.events : [];
  if (!events.length) return '<div class="match-empty-line">暂无事件明细</div>';
  return `<div class="match-incidents">${events
    .slice(-5)
    .reverse()
    .map((event) => {
      const minute = event.minute ? `${escapeHtml(event.minute)} · ` : "";
      const title = event.text || event.type || "比赛事件";
      const people = event.participants?.length ? ` · ${event.participants.map(escapeHtml).join("、")}` : "";
      return `<div class="match-incident"><span class="match-minute">${minute}</span><span>${escapeHtml(title)}${people}</span></div>`;
    })
    .join("")}</div>`;
}

function renderMatchCenter() {
  const target = $("#live-match-center");
  const meta = $("#match-center-meta");
  if (!target || !meta) return;
  const current = state.snapshot.current_data || {};
  const center = current.match_center || {};
  const live = Array.isArray(center.live) ? center.live : [];
  const recent = Array.isArray(center.recent_finished) ? center.recent_finished : [];
  const observed = center.retrieved_at ? ` · 观测 ${formatDateTime(center.retrieved_at)}` : "";
  meta.textContent = `${formatNumber(center.incident_observation_count ?? 0)} 场事件 · ${formatNumber(center.stats_observation_count ?? 0)} 场统计${observed}`;

  const renderCard = (row) => {
    const score = row.score && typeof row.score.home === "number" && typeof row.score.away === "number"
      ? `${row.score.home} — ${row.score.away}`
      : "—";
    const stateLabel = row.status === "live"
      ? `${row.status_text || "进行中"}${row.clock ? ` · ${row.clock}` : ""}${row.period ? ` · 第 ${row.period} 节` : ""}`
      : "最近完赛 · 事件仅作审计";
    return `<article class="match-card">
      <div class="match-card-head"><span class="tag ${row.status === "live" ? "tag--live" : ""}">${escapeHtml(stateLabel)}</span><span>${escapeHtml(formatDateTime(row.kickoff_at))}</span></div>
      <div class="match-card-score"><span>${escapeHtml(row.home_team || "—")}</span><strong>${escapeHtml(score)}</strong><span>${escapeHtml(row.away_team || "—")}</span></div>
      ${matchCenterStats(row)}
      ${matchCenterIncidents(row)}
      <div class="match-card-foot">ESPN event summary · 采集 ${escapeHtml(formatDateTime(row.observed_at))} · 不进入赛前模型</div>
    </article>`;
  };

  if (!live.length && !recent.length) {
    target.innerHTML = '<div class="empty empty--compact"><strong>当前没有可展示的实时事件</strong><span>下一轮 ESPN 事件摘要轮询会继续更新；赛中事件和统计始终与赛前预测隔离。</span></div>';
    return;
  }
  const recentBlock = recent.length
    ? `<div class="match-center-subhead"><span>最近完赛事件</span><small>仅用于赛后审计，不回填赛前信息</small></div><div class="match-center-grid">${recent.map(renderCard).join("")}</div>`
    : "";
  const liveBlock = live.length
    ? `<div class="match-center-subhead"><span>进行中</span><small>${formatNumber(live.length)} 场</small></div><div class="match-center-grid">${live.map(renderCard).join("")}</div>`
    : '<div class="match-empty-line">当前无进行中的比赛</div>';
  target.innerHTML = `${liveBlock}${recentBlock}`;
}

function renderProspectiveStatus() {
  const target = $("#prospective-freeze-status");
  if (!target) return;

  const prospective = state.snapshot.prospective_evaluation || {};
  const diagnostics = prospective.blocked_diagnostics || {};
  const nextFreezes = Array.isArray(diagnostics.next_freezes) ? diagnostics.next_freezes : [];
  const cycleStatus = prospective.cycle_status || "unavailable";
  const statusLabel =
    cycleStatus === "pending_prospective_window"
      ? "等待真实赛果"
      : cycleStatus === "passed"
        ? "前瞻窗口已通过"
        : cycleStatus === "unavailable"
          ? "周期证据不可用"
          : cycleStatus;
  const header = `<div class="source-row">
    <strong>当前前瞻周期</strong>
    <span class="source-status"><span class="dot dot--${cycleStatus === "passed" ? "good" : "warning"}" aria-hidden="true"></span>${escapeHtml(statusLabel)}</span>
    <span class="source-meta">周期观测截至 ${escapeHtml(formatDateTime(prospective.cycle_as_of))} · 已评分 ${formatNumber(prospective.scored_n ?? 0)} 场 · 待评分 ${formatNumber(prospective.pending_n ?? 0)} 场</span>
    <span class="source-meta">这些时间只用于提示下一次采集窗口，不会改变已锁定模型或补写预测。</span>
  </div>`;

  if (!nextFreezes.length) {
    target.innerHTML = `${header}<div class="empty empty--compact"><strong>暂无下一冻结记录</strong><span>保持定时采集；到达冻结时间窗后才会产生可审计版本。</span></div>`;
    return;
  }

  const rows = nextFreezes
    .slice()
    .sort((left, right) => String(left.next_freeze_cutoff_at).localeCompare(String(right.next_freeze_cutoff_at)))
    .map((item) => {
      const league = leagueById(item.competition_id);
      const stage = FREEZE_STAGE_LABELS[item.next_freeze_stage] ?? item.next_freeze_stage;
      return `<div class="source-row prospective-freeze-row">
        <strong>${escapeHtml(league?.name_zh ?? item.competition_id)}</strong>
        <span class="source-status">${escapeHtml(stage)} · ${escapeHtml(formatDateTime(item.next_freeze_cutoff_at))}</span>
        <span class="source-meta">比赛 ${escapeHtml(item.next_fixture_id)} · 开赛 ${escapeHtml(formatDateTime(item.next_kickoff_at))}</span>
        <span class="source-meta">到达该截点后重新抓取；当前阻断只表示尚未观察到合法冻结时间，不代表预测为零概率。</span>
      </div>`;
    })
    .join("");
  target.innerHTML = `${header}${rows}`;
}

/* --------------------------------------------------------------- top status */

function renderStatus() {
  const current = state.snapshot.current_data;
  const prospective = state.snapshot.prospective_evaluation || {};
  const fresh = current?.status === "fresh";
  const offline = current?.offline_preview === true;
  const minutes = current?.age_seconds != null ? Math.floor(current.age_seconds / 60) : null;
  const age = minutes == null ? "" : minutes >= 60 ? ` · ${Math.floor(minutes / 60)} 小时前` : ` · ${minutes} 分钟前`;

  const freshnessLabel = offline ? "离线构建快照" : fresh ? "当前快照新鲜" : "当前快照已过期";
  const freshnessDot = offline ? "warning" : fresh ? "good" : "serious";
  $("#freshness-chip").innerHTML = `<span class="dot dot--${freshnessDot}" aria-hidden="true"></span><span>${freshnessLabel}${age}</span>`;

  const strictProductionAllowed = state.snapshot.strict_backtest?.overall?.production_allowed === true;
  const strictReportStale = state.snapshot.strict_backtest?.status === "stale";
  const prospectivePassed = prospective.status === "passed";
  const blocked = !strictProductionAllowed || !prospectivePassed;
  const pending = Number.isFinite(Number(prospective.pending_n)) ? Number(prospective.pending_n) : null;
  const gateDetail = blocked
    ? strictReportStale
      ? "关闭 · 严格报告待刷新"
      : pending != null && pending > 0
      ? `关闭 · 前瞻待评分 ${formatNumber(pending)}`
      : "关闭 · 研究对照"
    : "开放";
  $("#gate-chip").innerHTML = `<span class="dot dot--${blocked ? "warning" : "good"}" aria-hidden="true"></span><span>生产门禁 ${gateDetail}</span>`;

  $("#rail-schema").textContent = `Schema ${state.snapshot.schema_version}`;
  $("#rail-generated").textContent = `生成于 ${formatDateTime(state.snapshot.generated_at)}`;
}

/* ------------------------------------------------------------------ controls */

function renderLeagueTabs() {
  const counts = {};
  for (const match of state.snapshot.matches) {
    counts[match.competition_id] = (counts[match.competition_id] ?? 0) + 1;
  }
  const tabs = [{ id: "all", name_zh: "全部" }, ...state.snapshot.competitions];
  $("#league-tabs").innerHTML = tabs
    .map(
      (league) => `
        <button class="tab" type="button" data-league="${escapeHtml(league.id)}"
                aria-pressed="${state.competition === league.id}">
          ${escapeHtml(league.name_zh)}
          <span class="tab-count">${formatNumber(league.id === "all" ? state.snapshot.matches.length : counts[league.id] ?? 0)}</span>
        </button>`,
    )
    .join("");
  $("#league-tabs")
    .querySelectorAll("[data-league]")
    .forEach((button) => {
      button.addEventListener("click", () => {
        state.competition = button.dataset.league;
        state.visible = 30;
        renderLeagueTabs();
        renderMatches();
      });
    });
}

function renderSeasonOptions() {
  const seasons = [...new Set(state.snapshot.matches.map((match) => match.season))].sort().reverse();
  $("#season-filter").innerHTML = [
    '<option value="all">全部</option>',
    ...seasons.map((season) => `<option value="${escapeHtml(season)}">${escapeHtml(season)}</option>`),
  ].join("");
}

function bindControls() {
  const rerender = () => {
    state.visible = 30;
    renderMatches();
  };
  $("#season-filter").addEventListener("change", (event) => {
    state.season = event.target.value;
    rerender();
  });
  $("#status-filter").addEventListener("change", (event) => {
    state.status = event.target.value;
    rerender();
  });
  $("#date-filter").addEventListener("change", (event) => {
    state.date = event.target.value;
    rerender();
  });
  $("#team-search").addEventListener("input", (event) => {
    state.query = event.target.value.trim();
    rerender();
  });
  $("#clear-filters").addEventListener("click", () => {
    Object.assign(state, { competition: "all", season: "all", status: "all", date: "", query: "", visible: 30 });
    $("#season-filter").value = "all";
    $("#status-filter").value = "all";
    $("#date-filter").value = "";
    $("#team-search").value = "";
    renderLeagueTabs();
    renderMatches();
  });
  $("#load-more").addEventListener("click", () => {
    state.visible += 25;
    renderMatches();
  });
  $("#fixture-list").addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-open-match]");
    if (!trigger) return;
    openMatchDrawer(trigger.dataset.openMatch, trigger);
  });
  $("#match-drawer-close")?.addEventListener("click", closeMatchDrawer);
  $("#match-drawer-scrim")?.addEventListener("click", closeMatchDrawer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.activeMatchId) closeMatchDrawer();
  });
  $("#toggle-table").addEventListener("click", (event) => {
    const hidden = $("#metric-table-panel").classList.toggle("is-hidden");
    event.currentTarget.setAttribute("aria-expanded", String(!hidden));
    event.currentTarget.textContent = hidden ? "查看数据表" : "收起数据表";
  });
  $("#theme-toggle").addEventListener("click", () => {
    const root = document.documentElement;
    root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
  });

  const links = [...document.querySelectorAll(".rail-link")];
  const sections = links
    .map((link) => document.querySelector(link.getAttribute("href")))
    .filter(Boolean);
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        links.forEach((link) =>
          link.setAttribute("aria-current", String(link.getAttribute("href") === `#${entry.target.id}`)),
        );
      }
    },
    { rootMargin: "-20% 0px -70% 0px" },
  );
  sections.forEach((section) => observer.observe(section));
}

/* ------------------------------------------------------------------ tooltips */

const tooltip = $("#tooltip");
let hideTimer = null;

function showTooltip(event, html) {
  clearTimeout(hideTimer);
  tooltip.innerHTML = html;
  tooltip.dataset.show = "true";
  tooltip.setAttribute("aria-hidden", "false");
  const rect = tooltip.getBoundingClientRect();
  const anchor = event.currentTarget.getBoundingClientRect();
  const x = event.clientX || anchor.left + anchor.width / 2;
  const y = event.clientY || anchor.top;
  tooltip.style.left = `${Math.max(10, Math.min(x + 14, window.innerWidth - rect.width - 10))}px`;
  tooltip.style.top = `${Math.max(10, y - rect.height - 12)}px`;
}

function hideTooltip() {
  hideTimer = setTimeout(() => {
    tooltip.dataset.show = "false";
    tooltip.setAttribute("aria-hidden", "true");
  }, 100);
}

function bindTooltip(element, build) {
  element.addEventListener("mouseenter", (event) => showTooltip(event, build()));
  element.addEventListener("mousemove", (event) => showTooltip(event, build()));
  element.addEventListener("focus", (event) => showTooltip(event, build()));
  element.addEventListener("mouseleave", hideTooltip);
  element.addEventListener("blur", hideTooltip);
}

function tooltipRow(token, label, value) {
  const swatch = token ? `<span class="swatch swatch--${token}"></span>` : "<span></span>";
  return `<div class="tooltip-row">${swatch}<span>${label}</span><b>${value}</b></div>`;
}

function attachLadderTooltips(rows) {
  document.querySelectorAll(".ladder-row").forEach((element) => {
    const row = rows.find((item) => item.id === element.dataset.league);
    if (!row) return;
    bindTooltip(element, () =>
      [
        `<div class="tooltip-title">${escapeHtml(row.nameZh)} · ${escapeHtml(row.selected)}</div>`,
        tooltipRow("model", "模型 Brier", row.metrics.brier_score.toFixed(4)),
        row.market ? tooltipRow("market", "市场 Brier", row.market.brier_score.toFixed(4)) : "",
        row.historical ? tooltipRow("baseline", "历史频率", row.historical.brier_score.toFixed(4)) : "",
        tooltipRow("", "留出样本", formatNumber(row.metrics.sample_n ?? 0)),
        tooltipRow("", "校准误差 ECE", (row.metrics.ece ?? 0).toFixed(4)),
        tooltipRow("", "数据截止", formatDate(row.dataCutoff)),
      ].join(""),
    );
  });
}

function attachFoldTooltips(rows) {
  document.querySelectorAll("[data-fold]").forEach((circle) => {
    const row = rows.find((item) => item.id === circle.dataset.league);
    const fold = row?.folds.find((item) => item.fold === Number(circle.dataset.fold));
    if (!fold) return;
    circle.setAttribute("tabindex", "0");
    bindTooltip(circle, () =>
      [
        `<div class="tooltip-title">${escapeHtml(row.nameZh)} · 第 ${fold.fold} 折</div>`,
        tooltipRow("", "窗口", `${formatDate(fold.start_at)} → ${formatDate(fold.end_at)}`),
        tooltipRow("", "样本", formatNumber(fold.sample_n)),
        tooltipRow("model", "Brier", fold.brier_score.toFixed(4)),
        tooltipRow("", "Log loss", fold.log_loss.toFixed(4)),
        tooltipRow("", "ECE", fold.ece.toFixed(4)),
      ].join(""),
    );
  });
}

/* ---------------------------------------------------------------------- boot */

function failBoot(message) {
  const notice = $("#boot-notice");
  if (notice) {
    notice.classList.add("notice--error");
    notice.querySelector("svg").style.stroke = "var(--critical)";
    notice.querySelector("span").textContent = message;
  }
  $("#freshness-chip").innerHTML = '<span class="dot dot--critical"></span><span>数据不可用</span>';
}

async function boot() {
  let snapshot;
  let predictions;
  const offlineSnapshot = window.__MATCHLINE_OFFLINE_SNAPSHOT__;
  const offlinePredictions = window.__MATCHLINE_OFFLINE_PREDICTIONS__;
  const useOffline = window.location.protocol === "file:" && offlineSnapshot && offlinePredictions;

  if (useOffline) {
    snapshot = offlineSnapshot;
    predictions = offlinePredictions;
  } else {
    try {
      const [snapshotResponse, predictionResponse, researchResponse] = await Promise.all([
        fetch("/api/v1/snapshot", { headers: { Accept: "application/json" } }),
        fetch("/api/v1/predictions", { headers: { Accept: "application/json" } }),
        fetch("/api/v1/research-predictions", { headers: { Accept: "application/json" } }).catch(() => null),
      ]);
      if (!snapshotResponse.ok || !predictionResponse.ok) {
        throw new Error(`HTTP ${snapshotResponse.status}/${predictionResponse.status}`);
      }
      snapshot = await snapshotResponse.json();
      predictions = await predictionResponse.json();
      const researchPayload = researchResponse?.ok ? await researchResponse.json() : null;
      if (Array.isArray(researchPayload?.research_predictions?.predictions)) {
        predictions = {
          ...predictions,
          research_predictions: researchPayload.research_predictions.predictions,
        };
      }
    } catch (error) {
      if (offlineSnapshot && offlinePredictions) {
        snapshot = offlineSnapshot;
        predictions = offlinePredictions;
      } else {
        failBoot(`平台数据读取失败：${error.message}。请使用 python -m league_platform.app 启动服务。`);
        return;
      }
    }
  }

  state.snapshot = snapshot;
  // Offline Sites bundles carry a second, explicitly research-only
  // projection. Keep it separate from the locked public rows so the card can
  // label it, while online/API consumers continue to expose only the public
  // prediction contract.
  state.predictions = {
    ...predictions,
    research_predictions: Array.isArray(snapshot.research_predictions)
      ? snapshot.research_predictions
      : Array.isArray(predictions.research_predictions)
        ? predictions.research_predictions
        : [],
  };
  state.blockedFixtures = new Set((predictions.blocked || []).map((item) => item.fixture_id));

  const rows = state.snapshot.competitions.map(evalRow);
  $("#boot-notice")?.remove();
  renderHero(rows);
  renderLadder(rows);
  renderMetricTable(rows);
  renderFolds(rows);
  attachFoldTooltips(rows);
  renderLeagueTabs();
  renderSeasonOptions();
  renderMatches();
  renderSources();
  renderMatchCenter();
  renderProspectiveStatus();
  renderStatus();
  bindControls();
}

boot();
