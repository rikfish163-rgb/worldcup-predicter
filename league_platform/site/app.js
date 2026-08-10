const state = {
  snapshot: null,
  predictions: new Map(),
  blockedFixtures: new Set(),
  competition: "all",
  season: "all",
  status: "all",
  date: "",
  query: "",
  visible: 24,
};

const statusLabels = {
  finished: "完赛",
  upcoming: "未赛",
  live: "进行中",
  postponed: "延期",
  cancelled: "取消",
};

const sourceStatusLabels = {
  fresh: "实时",
  delayed: "延迟",
  stale: "过期",
  unavailable: "不可用",
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

function formatNumber(value) {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function formatDate(value) {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date(value));
}

function leagueById(id) {
  return state.snapshot.competitions.find((league) => league.id === id);
}

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

function renderLeagueTabs() {
  const tabs = [
    { id: "all", name_zh: "全部", source_status: "fresh" },
    ...state.snapshot.competitions,
  ];
  $("#league-tabs").innerHTML = tabs
    .map(
      (league) => `
        <button
          class="league-tab"
          type="button"
          data-league="${escapeHtml(league.id)}"
          data-status="${escapeHtml(league.source_status)}"
          aria-pressed="${state.competition === league.id}"
        >${escapeHtml(league.name_zh)}</button>
      `,
    )
    .join("");
  document.querySelectorAll("[data-league]").forEach((button) => {
    button.addEventListener("click", () => {
      state.competition = button.dataset.league;
      state.visible = 24;
      renderLeagueTabs();
      renderMatches();
    });
  });
}

function renderSeasonOptions() {
  const seasons = [...new Set(state.snapshot.matches.map((match) => match.season))].sort().reverse();
  $("#season-filter").innerHTML = [
    '<option value="all">全部赛季</option>',
    ...seasons.map((season) => `<option value="${escapeHtml(season)}">${escapeHtml(season)}</option>`),
  ].join("");
}

function renderSummary() {
  const { summary, competitions, matches } = state.snapshot;
  const latestFinished = matches.find((match) => match.status === "finished")?.kickoff_at;
  const current = state.snapshot.current_data;
  $("#summary-matches").textContent = formatNumber(summary.finished_matches);
  $("#summary-leagues").textContent = `${summary.available_competitions} / ${competitions.length}`;
  $("#summary-latest").textContent = latestFinished ? formatDate(latestFinished) : "暂无";
  $("#summary-models").textContent = `${summary.evaluated_models} / ${competitions.length}`;
  $("#hero-coverage").textContent = `${summary.available_competitions} / ${competitions.length}`;
  $("#hero-freshness").textContent =
    current?.status === "fresh"
      ? `当前赛程已同步 · 截至 ${formatDate(current.as_of)}`
      : current?.message || "没有可验证的当前赛程";
  $("#footer-version").textContent = `Schema ${state.snapshot.schema_version}`;

  const hasFresh = current?.status === "fresh";
  const header = $("#header-status");
  header.innerHTML = `<span class="status-dot ${hasFresh ? "" : "status-dot--stale"}"></span>${
    hasFresh ? "当前赛程已同步" : "当前数据不可用"
  }`;
}

function marketBlock(match) {
  const prediction = state.predictions.get(match.id);
  if (prediction) {
    const probabilities = prediction.dixon_coles_probability;
    const labels = [
      ["主", probabilities.home],
      ["平", probabilities.draw],
      ["客", probabilities.away],
    ];
    return `
      <span class="market-label">研究预测 · Dixon-Coles · as-of ${formatDate(prediction.as_of)}</span>
      ${labels
        .map(
          ([label, value]) => `
            <div class="probability-line">
              <span>${label}</span>
              <span class="probability-track" aria-hidden="true">
                <span class="probability-fill" style="width:${Math.round(value * 100)}%"></span>
              </span>
              <span class="probability-value">${(value * 100).toFixed(1)}%</span>
            </div>
          `,
        )
        .join("")}
      <span class="market-label">预期进球 ${prediction.expected_goals.home.toFixed(2)} : ${prediction.expected_goals.away.toFixed(2)} · ${
        prediction.feature_coverage.recent_xg ? "含 Understat 近况" : "无当前 xG，已降级"
      }</span>
      <span class="market-label">${
        prediction.market_probability
          ? `当前市场对照 主 ${(prediction.market_probability.home * 100).toFixed(1)}% · 平 ${(
              prediction.market_probability.draw * 100
            ).toFixed(1)}% · 客 ${(prediction.market_probability.away * 100).toFixed(1)}%`
          : "没有可验证的当前市场快照；伤停与首发也未确认，生产门禁保持关闭"
      }</span>
    `;
  }
  if (state.blockedFixtures.has(match.id)) {
    return '<p class="market-label">新升班或历史样本不足，预测已阻断</p>';
  }
  if (!match.market_probability) {
    return '<p class="market-label">该场没有可验证的市场概率</p>';
  }
  const labels = [
    ["主", match.market_probability.home],
    ["平", match.market_probability.draw],
    ["客", match.market_probability.away],
  ];
  return `
    <span class="market-label">历史市场去水概率</span>
    ${labels
      .map(
        ([label, value]) => `
          <div class="probability-line">
            <span>${label}</span>
            <span class="probability-track" aria-hidden="true">
              <span class="probability-fill" style="width:${Math.round(value * 100)}%"></span>
            </span>
            <span class="probability-value">${(value * 100).toFixed(1)}%</span>
          </div>
        `,
      )
      .join("")}
  `;
}

function renderMatches() {
  const matches = filteredMatches();
  const visible = matches.slice(0, state.visible);
  $("#match-count").textContent = `共 ${formatNumber(matches.length)} 场，显示 ${formatNumber(visible.length)} 场`;
  $("#match-list").innerHTML = visible.length
    ? visible
        .map((match) => {
          const league = leagueById(match.competition_id);
          const score = match.score ?? { home: "—", away: "—" };
          return `
            <article class="match-row" aria-label="${escapeHtml(match.home_team)} 对 ${escapeHtml(
              match.away_team,
            )}">
              <div class="match-meta">
                <span class="match-league">${escapeHtml(league?.name_zh ?? match.competition_id)}</span>
                <time class="match-time" datetime="${escapeHtml(match.kickoff_at)}">${formatDate(
                  match.kickoff_at,
                )}</time>
                <span class="match-season">${escapeHtml(match.season)} · ${escapeHtml(
                  statusLabels[match.status] ?? match.status,
                )}</span>
              </div>
              <div class="match-teams">
                <div class="team-line">
                  <span class="team-name">${escapeHtml(match.home_team)}</span>
                  <strong class="team-score">${escapeHtml(score.home)}</strong>
                </div>
                <div class="team-line">
                  <span class="team-name">${escapeHtml(match.away_team)}</span>
                  <strong class="team-score">${escapeHtml(score.away)}</strong>
                </div>
              </div>
              <div class="match-market">
                ${marketBlock(match)}
                <span class="source-tag">来源 ${escapeHtml(match.source.name)} · ${escapeHtml(
                  match.source.file ?? match.source.native_fixture_id ?? "已记录原始快照",
                )}</span>
              </div>
            </article>
          `;
        })
        .join("")
    : `
      <div class="empty-state">
        <strong>没有符合条件的比赛</strong>
        <p>更换联赛、赛季或球队关键词，或清除筛选。</p>
      </div>
    `;
  $("#load-more").hidden = visible.length >= matches.length;
}

function renderSourceHealth() {
  $("#source-list").innerHTML = state.snapshot.competitions
    .map(
      (league) => `
        <div class="source-item">
          <strong>${escapeHtml(league.name_zh)}</strong>
          <span class="source-status source-status--${escapeHtml(
            league.current_source_status ?? league.source_status
          )}">${escapeHtml(
            sourceStatusLabels[league.current_source_status ?? league.source_status] ??
              league.current_source_status ??
              league.source_status,
          )}</span>
          <small>当前赛程 ${formatNumber(league.current_fixture_count ?? 0)} 场 · xG 球队 ${formatNumber(
            league.current_xg_team_count ?? 0,
          )} 支。历史训练集：${escapeHtml(league.source_message)}</small>
        </div>
      `,
    )
    .join("");
}

function renderModelHealth() {
  $("#model-table-body").innerHTML = state.snapshot.competitions
    .map((league) => {
      const metrics = league.model_health.selected_metrics ?? league.model_health;
      const candidate = league.model_health.selected_candidate ?? league.model_health.model;
      return `
        <tr>
          <th scope="row">${escapeHtml(league.name_zh)}</th>
          <td class="model-pending">${
            league.model_health.status === "evaluated" ? "已留出评估" : "待严格回测"
          }</td>
          <td>${formatNumber(metrics.sample_n)}</td>
          <td>${metrics.brier_score ?? "—"}</td>
          <td>${metrics.log_loss ?? "—"}</td>
          <td>${
            league.model_health.status === "evaluated"
              ? `${escapeHtml(league.model_health.evaluation_season)} 留出赛季 · ${escapeHtml(candidate)} · 研究基线`
              : escapeHtml(league.model_health.message)
          }</td>
        </tr>
      `;
    })
    .join("");
}

function bindControls() {
  $("#season-filter").addEventListener("change", (event) => {
    state.season = event.target.value;
    state.visible = 24;
    renderMatches();
  });
  $("#status-filter").addEventListener("change", (event) => {
    state.status = event.target.value;
    state.visible = 24;
    renderMatches();
  });
  $("#date-filter").addEventListener("change", (event) => {
    state.date = event.target.value;
    state.visible = 24;
    renderMatches();
  });
  $("#team-search").addEventListener("input", (event) => {
    state.query = event.target.value.trim();
    state.visible = 24;
    renderMatches();
  });
  $("#clear-filters").addEventListener("click", () => {
    state.competition = "all";
    state.season = "all";
    state.status = "all";
    state.date = "";
    state.query = "";
    state.visible = 24;
    $("#season-filter").value = "all";
    $("#status-filter").value = "all";
    $("#date-filter").value = "";
    $("#team-search").value = "";
    renderLeagueTabs();
    renderMatches();
  });
  $("#load-more").addEventListener("click", () => {
    state.visible += 24;
    renderMatches();
  });
}

async function boot() {
  try {
    const [snapshotResponse, predictionResponse] = await Promise.all([
      fetch("/api/v1/snapshot", { headers: { Accept: "application/json" } }),
      fetch("/api/v1/predictions", { headers: { Accept: "application/json" } }),
    ]);
    if (!snapshotResponse.ok || !predictionResponse.ok) {
      throw new Error(`HTTP ${snapshotResponse.status}/${predictionResponse.status}`);
    }
    state.snapshot = await snapshotResponse.json();
    const predictionPayload = await predictionResponse.json();
    state.predictions = new Map(
      predictionPayload.predictions.map((prediction) => [prediction.fixture_id, prediction]),
    );
    state.blockedFixtures = new Set(
      predictionPayload.blocked.map((item) => item.fixture_id),
    );
    $("#loading-notice").remove();
    renderLeagueTabs();
    renderSeasonOptions();
    renderSummary();
    renderMatches();
    renderSourceHealth();
    renderModelHealth();
    bindControls();
  } catch (error) {
    const notice = $("#loading-notice");
    notice.className = "notice notice--error";
    notice.textContent = `平台数据读取失败：${error.message}。请使用 python -m league_platform.app 启动服务。`;
    $("#header-status").innerHTML = '<span class="status-dot status-dot--stale"></span>数据不可用';
  }
}

boot();
