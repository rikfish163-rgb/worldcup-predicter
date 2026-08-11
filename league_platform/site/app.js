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
  const mh = league.model_health || {};
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
    ["model", "模型", stats.model, "online_dixon_coles_v1"],
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
  $("#tile-gate").textContent = "关闭";
  $("#tile-gate").style.color = "var(--text-warning)";
  $("#tile-gate-foot").textContent = "缺当前伤停与确认首发";
}

/* ------------------------------------------------------- calibration ladder */

function renderLadder(rows) {
  const values = rows
    .flatMap((row) => [row.metrics.brier_score, row.market?.brier_score, row.historical?.brier_score])
    .filter((value) => typeof value === "number");
  const lo = Math.floor(Math.min(...values) * 100) / 100;
  const hi = Math.ceil(Math.max(...values) * 100) / 100;
  const pos = (value) => ((value - lo) / (hi - lo)) * 100;

  $("#ladder").innerHTML = rows
    .slice()
    .sort((a, b) => a.metrics.brier_score - b.metrics.brier_score)
    .map((row) => {
      const xModel = pos(row.metrics.brier_score);
      const xMarket = row.market ? pos(row.market.brier_score) : null;
      const xHistory = row.historical ? pos(row.historical.brier_score) : null;
      const delta = row.market ? row.metrics.brier_score - row.market.brier_score : null;
      const relative = row.market ? (delta / row.market.brier_score) * 100 : null;

      const deltaClass =
        delta === null ? "ladder-delta--none" : delta > 0 ? "ladder-delta--worse" : "ladder-delta--better";
      const deltaText =
        delta === null ? "无市场基线" : `${delta > 0 ? "+" : ""}${relative.toFixed(1)}%`;

      const gapStyle =
        xMarket === null
          ? "display:none"
          : `left:${Math.min(xMarket, xModel)}%;width:${Math.abs(xModel - xMarket)}%`;

      const label = row.market
        ? `${row.nameZh}，模型 Brier ${row.metrics.brier_score.toFixed(3)}，市场 ${row.market.brier_score.toFixed(3)}，相对差 ${relative.toFixed(1)}%`
        : `${row.nameZh}，模型 Brier ${row.metrics.brier_score.toFixed(3)}，无市场基线`;

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

function probabilityBlock(match) {
  const prediction = state.predictions?.predictions?.find((item) => item.fixture_id === match.id);
  const currentMarket = (match.current_features || {}).market;

  let probabilities = null;
  let title = "";
  let stamp = "";

  if (prediction?.dixon_coles_probability) {
    probabilities = prediction.dixon_coles_probability;
    title = "模型 · research_only";
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
    const reason = state.blockedFixtures.has(match.id)
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

  const roles = state.snapshot.current_data?.roles || {};
  $("#role-list").innerHTML = [
    ["fixtures_and_results", "赛程结果"],
    ["recent_xg_and_form", "近况 xG"],
    ["historical_training", "历史训练"],
    ["current_market", "当前市场"],
    ["injuries_and_lineups", "伤停首发"],
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

/* --------------------------------------------------------------- top status */

function renderStatus() {
  const current = state.snapshot.current_data;
  const fresh = current?.status === "fresh";
  const minutes = current?.age_seconds != null ? Math.floor(current.age_seconds / 60) : null;
  const age = minutes == null ? "" : minutes >= 60 ? ` · ${Math.floor(minutes / 60)} 小时前` : ` · ${minutes} 分钟前`;

  $("#freshness-chip").innerHTML = `<span class="dot dot--${fresh ? "good" : "serious"}" aria-hidden="true"></span><span>${
    fresh ? "当前快照新鲜" : "当前快照已过期"
  }${age}</span>`;

  const blocked = (state.predictions?.predictions?.length ?? 0) === 0;
  $("#gate-chip").innerHTML = `<span class="dot dot--${blocked ? "warning" : "good"}" aria-hidden="true"></span><span>生产门禁 ${
    blocked ? "关闭" : "研究对照"
  }</span>`;

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
  try {
    const [snapshotResponse, predictionResponse] = await Promise.all([
      fetch("/api/v1/snapshot", { headers: { Accept: "application/json" } }),
      fetch("/api/v1/predictions", { headers: { Accept: "application/json" } }),
    ]);
    if (!snapshotResponse.ok || !predictionResponse.ok) {
      throw new Error(`HTTP ${snapshotResponse.status}/${predictionResponse.status}`);
    }
    snapshot = await snapshotResponse.json();
    predictions = await predictionResponse.json();
  } catch (error) {
    failBoot(`平台数据读取失败：${error.message}。请使用 python -m league_platform.app 启动服务。`);
    return;
  }

  state.snapshot = snapshot;
  state.predictions = predictions;
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
  renderStatus();
  bindControls();
}

boot();
