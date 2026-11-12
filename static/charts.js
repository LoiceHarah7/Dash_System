const chartData = JSON.parse(document.getElementById("chart-data").textContent);
const codebookLabels = JSON.parse(
  document.getElementById("label-map-data")?.textContent || "{}",
);
const allDatasetPids = JSON.parse(
  document.getElementById("all-pids-data")?.textContent || "[]",
);
const chartColors = ["#7057df", "#4b91d2", "#35a778", "#e5a43b", "#e16e78", "#42aab0", "#9470c9", "#81934b", "#df8650", "#7181aa"];
const svgNamespace = "http://www.w3.org/2000/svg";
const CHART_WIDTH = 800;
const ROW_HEIGHT = 38;
let chartLanguage = "en";
const categoryFilters = new Map();
const chartShowAllMap = new Map();

function sliceVariant(variant, limit) {
  if (!variant || !variant.labels || variant.labels.length <= limit) return variant;
  const result = {
    ...variant,
    labels: variant.labels.slice(0, limit),
  };
  if (variant.labels_en) result.labels_en = variant.labels_en.slice(0, limit);
  if (variant.labels_ny) result.labels_ny = variant.labels_ny.slice(0, limit);
  if (variant.values) result.values = variant.values.slice(0, limit);
  if (variant.codes) result.codes = variant.codes.slice(0, limit);
  if (variant.pids) result.pids = variant.pids.slice(0, limit);
  return result;
}

/* ---------- helpers ---------- */

function element(name, attributes = {}, text = null) {
  const node = document.createElementNS(svgNamespace, name);
  for (const [key, value] of Object.entries(attributes)) {
    node.setAttribute(key, String(value));
  }
  if (text !== null) node.textContent = text;
  return node;
}

function shorten(text, max) {
  text = String(text);
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function chartSvg(host, label, height) {
  host.replaceChildren();
  const svg = element("svg", {
    viewBox: `0 0 ${CHART_WIDTH} ${height}`,
    role: "img",
    "aria-label": label,
    preserveAspectRatio: "xMidYMid meet",
  });
  svg.classList.add("chart-svg");
  host.append(svg);
  return svg;
}

/* ---------- drawing ---------- */

function drawGrid(svg, maximum, left, top, width, height) {
  const ticks = 5;
  for (let index = 0; index <= ticks; index += 1) {
    const value = (maximum * (ticks - index)) / ticks;
    const y = top + (height * index) / ticks;
    svg.append(
      element("line", { x1: left, y1: y, x2: left + width, y2: y, class: "chart-gridline" }),
      element("text", { x: left - 12, y: y + 5, "text-anchor": "end", class: "chart-tick" }, Math.round(value).toLocaleString()),
    );
  }
}

function drawAxisLabel(svg, x, y, text, rotate, maxChars) {
  const attrs = { x, y, class: "chart-tick", "text-anchor": rotate ? "end" : "middle" };
  if (rotate) attrs.transform = `rotate(-35 ${x} ${y})`;
  const node = element("text", attrs, shorten(text, maxChars));
  node.append(element("title", {}, text)); // full label on hover
  svg.append(node);
}

function drawVerticalBars(svg, labels, values, pids) {
  const left = 66;
  const top = 26;
  const width = 710;
  const height = 255;
  const maximum = Math.max(...values, 1);
  drawGrid(svg, maximum, left, top, width, height);
  const slot = width / Math.max(labels.length, 1);
  const rotate = slot < 80;
  const maxChars = rotate ? 18 : Math.max(6, Math.floor(slot / 8));
  values.forEach((value, index) => {
    const barHeight = (value / maximum) * (height - 4);
    const barWidth = Math.min(60, slot * 0.66);
    const x = left + index * slot + (slot - barWidth) / 2;
    const y = top + height - barHeight;
    const bar = element("rect", { x, y, width: barWidth, height: barHeight, rx: 6, class: "chart-bar", fill: chartColors[index % chartColors.length] });
    if (pids && pids[index]) {
      bar.style.cursor = "pointer";
      bar.addEventListener("click", () => showPidsModal(null, labels[index], pids[index]));
    }
    svg.append(bar);
    if (slot >= 30) {
      const valText = element("text", { x: x + barWidth / 2, y: Math.max(y - 8, top + 10), "text-anchor": "middle", class: "chart-value" }, value.toLocaleString());
      if (pids && pids[index]) { valText.style.cursor = "pointer"; valText.addEventListener("click", () => showPidsModal(null, labels[index], pids[index])); }
      svg.append(valText);
    }
    drawAxisLabel(svg, x + barWidth / 2, top + height + 24, labels[index], rotate, maxChars);
  });
}

function drawHorizontalBars(svg, labels, values, pids) {
  const left = 230;
  const top = 14;
  const width = 470;
  const maximum = Math.max(...values, 1);
  values.forEach((value, index) => {
    const y = top + index * ROW_HEIGHT;
    const barWidth = (value / maximum) * width;
    const barHeight = ROW_HEIGHT * 0.58;
    const barY = y + (ROW_HEIGHT - barHeight) / 2;
    const label = element("text", { x: left - 14, y: y + ROW_HEIGHT * 0.64, "text-anchor": "end", class: "chart-tick" }, shorten(labels[index], 30));
    label.append(element("title", {}, labels[index]));
    const bar = element("rect", { x: left, y: barY, width: Math.max(barWidth, 3), height: barHeight, rx: 5, fill: chartColors[index % chartColors.length] });
    const countText = element("text", { x: Math.min(left + barWidth + 10, CHART_WIDTH - 50), y: y + ROW_HEIGHT * 0.64, class: "chart-value" }, value.toLocaleString());
    if (pids && pids[index]) {
      const pidList = pids[index];
      bar.style.cursor = "pointer";
      bar.addEventListener("click", () => showPidsModal(null, labels[index], pidList));
      countText.style.cursor = "pointer";
      countText.addEventListener("click", () => showPidsModal(null, labels[index], pidList));
    }
    svg.append(
      label,
      element("rect", { x: left, y: barY, width, height: barHeight, rx: 5, class: "chart-bar-track" }),
      bar,
      countText,
    );
  });
}

function drawLine(svg, labels, values) {
  const left = 66;
  const top = 26;
  const width = 700;
  const height = 255;
  const maximum = Math.max(...values, 1);
  drawGrid(svg, maximum, left, top, width, height);
  const points = values.map((value, index) => ({
    x: left + (values.length <= 1 ? width / 2 : (index / (values.length - 1)) * width),
    y: top + height - (value / maximum) * (height - 5),
  }));
  const path = points.map((point, index) => `${index ? "L" : "M"} ${point.x} ${point.y}`).join(" ");
  svg.append(element("path", { d: path, class: "chart-line" }));
  points.forEach((point, index) => {
    svg.append(element("circle", { cx: point.x, cy: point.y, r: 5, class: "chart-point" }));
    if (index === 0 || index === points.length - 1 || index % Math.ceil(points.length / 6) === 0) {
      svg.append(element("text", { x: point.x, y: top + height + 26, "text-anchor": "middle", class: "chart-tick" }, labels[index]));
    }
  });
}

function drawBoxPlot(svg, summary) {
  const [minimum, firstQuartile, median, thirdQuartile, maximum] = summary;
  const left = 60;
  const right = 740;
  const y = 145;
  const span = maximum - minimum || 1;
  const position = (value) => left + ((value - minimum) / span) * (right - left);
  const first = position(firstQuartile);
  const third = position(thirdQuartile);
  const middle = position(median);
  svg.append(
    element("line", { x1: left, y1: y, x2: right, y2: y, class: "box-whisker" }),
    element("line", { x1: left, y1: y - 22, x2: left, y2: y + 22, class: "box-cap" }),
    element("line", { x1: right, y1: y - 22, x2: right, y2: y + 22, class: "box-cap" }),
    element("rect", { x: first, y: y - 42, width: Math.max(third - first, 3), height: 84, rx: 8, class: "box-range" }),
    element("line", { x1: middle, y1: y - 42, x2: middle, y2: y + 42, class: "box-median" }),
  );
  [minimum, firstQuartile, median, thirdQuartile, maximum].forEach((value, index) => {
    const x = position(value);
    svg.append(
      element("line", { x1: x, y1: y + 48, x2: x, y2: y + 57, class: "box-cap" }),
      element("text", { x, y: y + 80, "text-anchor": index === 0 ? "start" : index === 4 ? "end" : "middle", class: "chart-value" }, Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })),
    );
  });
  svg.append(
    element("text", { x: CHART_WIDTH / 2, y: 32, "text-anchor": "middle", class: "box-explanation" }, "The box holds the middle half of the values. The thick line is the median."),
    element("text", { x: CHART_WIDTH / 2, y: 54, "text-anchor": "middle", class: "box-explanation" }, "The thin lines (whiskers) reach the smallest and largest values."),
  );
}

function drawLegend(svg, labels, values, x, height, pids) {
  const rowGap = 32;
  const startY = (height - labels.length * rowGap) / 2 + 22;
  labels.forEach((label, index) => {
    const y = startY + index * rowGap;
    const text = element("text", { x: x + 20, y, class: "chart-tick legend-text" }, `${shorten(label, 30)} · ${values[index].toLocaleString()}`);
    text.append(element("title", {}, label));
    if (pids && pids[index]) {
      const pidList = pids[index];
      text.style.cursor = "pointer";
      text.addEventListener("click", () => showPidsModal(null, label, pidList));
      const dot = element("circle", { cx: x, cy: y - 5, r: 7, fill: chartColors[index % chartColors.length] });
      dot.style.cursor = "pointer";
      dot.addEventListener("click", () => showPidsModal(null, label, pidList));
      svg.append(dot, text);
    } else {
      svg.append(
        element("circle", { cx: x, cy: y - 5, r: 7, fill: chartColors[index % chartColors.length] }),
        text,
      );
    }
  });
}

function drawDonut(svg, labels, values, height, pids) {
  const centerX = 190;
  const centerY = height / 2;
  const radius = 105;
  const circumference = 2 * Math.PI * radius;
  const total = values.reduce((sum, value) => sum + value, 0) || 1;
  let offset = 0;
  values.forEach((value, index) => {
    const segment = (value / total) * circumference;
    const circle = element("circle", {
      cx: centerX,
      cy: centerY,
      r: radius,
      fill: "none",
      stroke: chartColors[index % chartColors.length],
      "stroke-width": 44,
      "stroke-dasharray": `${segment} ${circumference - segment}`,
      "stroke-dashoffset": -offset,
      transform: `rotate(-90 ${centerX} ${centerY})`,
    });
    if (pids && pids[index]) {
      circle.style.cursor = "pointer";
      circle.addEventListener("click", () => showPidsModal(null, labels[index], pids[index]));
    }
    svg.append(circle);
    offset += segment;
  });
  svg.append(
    element("text", { x: centerX, y: centerY + 2, "text-anchor": "middle", class: "donut-total" }, total.toLocaleString()),
    element("text", { x: centerX, y: centerY + 26, "text-anchor": "middle", class: "chart-tick" }, "records"),
  );
  drawLegend(svg, labels, values, 390, height, pids);
}

function drawPie(svg, labels, values, height, pids) {
  const centerX = 200;
  const centerY = height / 2;
  const radius = 125;
  const total = values.reduce((sum, value) => sum + value, 0) || 1;
  if (values.length === 1) {
    const circle = element("circle", { cx: centerX, cy: centerY, r: radius, fill: chartColors[0] });
    if (pids && pids[0]) {
      circle.style.cursor = "pointer";
      circle.addEventListener("click", () => showPidsModal(null, labels[0], pids[0]));
    }
    svg.append(circle);
  }
  let angle = -Math.PI / 2;
  values.forEach((value, index) => {
    if (values.length === 1) return;
    const nextAngle = angle + (value / total) * Math.PI * 2;
    const startX = centerX + radius * Math.cos(angle);
    const startY = centerY + radius * Math.sin(angle);
    const endX = centerX + radius * Math.cos(nextAngle);
    const endY = centerY + radius * Math.sin(nextAngle);
    const largeArc = nextAngle - angle > Math.PI ? 1 : 0;
    const path = [
      `M ${centerX} ${centerY}`,
      `L ${startX} ${startY}`,
      `A ${radius} ${radius} 0 ${largeArc} 1 ${endX} ${endY}`,
      "Z",
    ].join(" ");
    const slice = element("path", {
      d: path,
      fill: chartColors[index % chartColors.length],
      stroke: "white",
      "stroke-width": 2,
    });
    if (pids && pids[index]) {
      slice.style.cursor = "pointer";
      slice.addEventListener("click", () => showPidsModal(null, labels[index], pids[index]));
    }
    svg.append(slice);
    angle = nextAngle;
  });
  drawLegend(svg, labels, values, 410, height, pids);
}

/* ---------- labels and filters ---------- */

function localizedLabels(variant) {
  const languageKey = chartLanguage === "ny" ? "labels_ny" : "labels_en";
  return variant[languageKey] || variant.labels || [];
}

function filteredVariant(variant, filterIndex) {
  if (filterIndex === undefined || filterIndex === "all") return variant;
  const index = Number(filterIndex);
  const result = { ...variant, labels: [localizedLabels(variant)[index]] };
  if (variant.labels_en) result.labels_en = [variant.labels_en[index]];
  if (variant.labels_ny) result.labels_ny = [variant.labels_ny[index]];
  if (variant.values) result.values = [variant.values[index]];
  if (variant.codes) result.codes = [variant.codes[index]];
  if (variant.pids) result.pids = [variant.pids[index]];
  return result;
}

/* ---------- rendering ---------- */

function renderChart(chartIndex, variantIndex) {
  const chart = chartData[chartIndex];
  const rawVariant = chart.variants[variantIndex];
  if (!rawVariant) return;

  const totalItems = rawVariant.labels ? rawVariant.labels.length : 0;
  const hasMoreThan10 = totalItems > 10;
  const showAll = chartShowAllMap.get(chartIndex) === true;

  // Update card toggle button if present
  const toggleBtn = document.querySelector(`[data-toggle-chart-limit="${chartIndex}"]`);
  if (toggleBtn) {
    if (hasMoreThan10) {
      toggleBtn.hidden = false;
      toggleBtn.setAttribute("aria-expanded", String(showAll));
      const span = toggleBtn.querySelector("span") || toggleBtn;
      span.textContent = showAll ? "Show first 10 ▴" : `Show all records (${totalItems}) ▾`;
    } else {
      toggleBtn.hidden = true;
    }
  }

  // Slice to 10 if not showing all
  const baseVariant = (hasMoreThan10 && !showAll) ? sliceVariant(rawVariant, 10) : rawVariant;

  const filterIndex = categoryFilters.get(chartIndex);
  const variant = filteredVariant(baseVariant, filterIndex);
  variant.labels = localizedLabels(variant);
  const host = document.querySelector(
    `[data-chart-host="${chartIndex}-${variantIndex}"]`,
  );
  if (!host) return;

  const count = variant.labels.length;
  const isVertical =
    variant.type === "histogram" ||
    (variant.type === "bar" && chart.kind === "date");
  const isRound = variant.type === "pie" || variant.type === "donut";
  const isHorizontal =
    !isVertical && !isRound && variant.type !== "line" && variant.type !== "box";

  let height = 360;
  if (variant.type === "box") height = 280;
  else if (isRound) height = Math.max(320, count * 32 + 70);
  else if (isHorizontal) height = Math.max(200, count * ROW_HEIGHT + 28);

  const svg = chartSvg(host, `${chart.name}, ${variant.label}`, height);

  if (isVertical) {
    drawVerticalBars(svg, variant.labels, variant.values, variant.pids);
  } else if (variant.type === "line") {
    drawLine(svg, variant.labels, variant.values);
  } else if (variant.type === "box") {
    drawBoxPlot(svg, variant.summary);
  } else if (variant.type === "pie") {
    drawPie(svg, variant.labels, variant.values, height, variant.pids);
  } else if (variant.type === "donut") {
    drawDonut(svg, variant.labels, variant.values, height, variant.pids);
  } else {
    drawHorizontalBars(svg, variant.labels, variant.values, variant.pids);
  }
}

function renderFrequencyTable(chartIndex) {
  const chart = chartData[chartIndex];
  if (!chart || chart.kind !== "categories" || !chart.variants.length) return;
  const rawVariant = chart.variants[0];
  const totalItems = rawVariant.labels ? rawVariant.labels.length : 0;
  const hasMoreThan10 = totalItems > 10;
  const showAll = chartShowAllMap.get(chartIndex) === true;
  const baseVariant = (hasMoreThan10 && !showAll) ? sliceVariant(rawVariant, 10) : rawVariant;

  const variant = filteredVariant(
    baseVariant,
    categoryFilters.get(chartIndex),
  );
  const labels = localizedLabels(variant);
  const host = document.querySelector(`[data-frequency-host="${chartIndex}"]`);
  if (!host) return;
  const table = document.createElement("table");
  table.className = "frequency-table";
  const head = document.createElement("thead");
  const headingRow = document.createElement("tr");
  for (const heading of [
    chartLanguage === "ny" ? "Yankho" : "Response",
    chartLanguage === "ny" ? "Chiwerengero" : "Count",
  ]) {
    const cell = document.createElement("th");
    cell.textContent = heading;
    headingRow.append(cell);
  }
  head.append(headingRow);
  const body = document.createElement("tbody");
  labels.forEach((label, index) => {
    const row = document.createElement("tr");
    const labelCell = document.createElement("td");
    const countCell = document.createElement("td");
    labelCell.textContent = label;
    const pidList = variant.pids && variant.pids[index];
    if (pidList && pidList.length) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "view-pids-btn";
      btn.textContent = `${variant.values[index].toLocaleString()} ↗`;
      btn.title = `View ${pidList.length} PIDs in this category`;
      btn.addEventListener("click", () => showPidsModal(chart.name, label, pidList));
      countCell.append(btn);
    } else {
      countCell.textContent = variant.values[index].toLocaleString();
    }
    row.append(labelCell, countCell);
    body.append(row);
  });
  table.append(head, body);
  host.replaceChildren(table);
}

function normalizeCodeForBrowser(value) {
  const number = Number(value);
  return Number.isFinite(number) ? String(number) : String(value);
}

function renderLanguage() {
  chartData.forEach((chart, chartIndex) => {
    chart.variants.forEach((variant, variantIndex) => {
      renderChart(chartIndex, variantIndex);
    });
    renderFrequencyTable(chartIndex);
  });
  document.querySelectorAll("[data-label-column]").forEach((cell) => {
    const raw = String(cell.dataset.rawValue ?? "").trim();
    if (!raw) return;
    const labelsByCode = codebookLabels[cell.dataset.labelColumn] || {};
    if (/\s+|,/.test(raw)) {
      const parts = raw
        .split(/[\s,]+/)
        .filter(Boolean)
        .map((code) => {
          const labels =
            labelsByCode[code] || labelsByCode[normalizeCodeForBrowser(code)];
          return labels?.[chartLanguage] || labels?.en || labels?.ny || code;
        });
      cell.textContent = parts.join(", ");
    } else {
      const labels =
        labelsByCode[raw] || labelsByCode[normalizeCodeForBrowser(raw)];
      cell.textContent =
        labels?.[chartLanguage] || labels?.en || labels?.ny || raw;
    }
  });
  document.querySelectorAll("[data-category-filter]").forEach((select) => {
    const chartIndex = Number(select.dataset.categoryFilter);
    const variant = chartData[chartIndex].variants[0];
    const selectedValue = select.value;
    select.replaceChildren();
    const all = document.createElement("option");
    all.value = "all";
    all.textContent = chartLanguage === "ny" ? "Mayankho onse" : "All responses";
    select.append(all);
    localizedLabels(variant).forEach((label, index) => {
      const option = document.createElement("option");
      option.value = String(index);
      option.textContent = label;
      select.append(option);
    });
    select.value = selectedValue;
  });
}

/* ---------- view mode, search and field picker ---------- */

let chartViewMode = "grouped";
const chartSearch = document.getElementById("chart-search-input");
const chartFieldSelect = document.getElementById("chart-field-select");
const chartSearchResults = document.getElementById("chart-search-results");
const relatedGroupOpenStates = new Map();

const chartCards = [...document.querySelectorAll(".chart-card[data-chart-name]")];

function updateFieldSelectOptions() {
  if (!chartFieldSelect) return;
  const previousValue = chartFieldSelect.value;
  chartFieldSelect.replaceChildren();
  const allOption = document.createElement("option");
  allOption.value = "";
  allOption.textContent =
    chartViewMode === "grouped" ? "All questions / categories" : "All fields";
  chartFieldSelect.append(allOption);

  const eligibleCards = chartCards.filter((card) => {
    if (chartViewMode === "grouped") {
      return card.dataset.isSubcolumn !== "true" && card.dataset.isBase !== "true";
    }
    return card.dataset.isGrouped !== "true";
  });

  eligibleCards.forEach((card) => {
    const option = document.createElement("option");
    option.value = card.dataset.chartName;
    option.textContent = card.dataset.chartName;
    chartFieldSelect.append(option);
  });

  if ([...chartFieldSelect.options].some((opt) => opt.value === previousValue)) {
    chartFieldSelect.value = previousValue;
  } else {
    chartFieldSelect.value = "";
  }
}

function applyViewMode() {
  chartCards.forEach((card) => {
    const isSub = card.dataset.isSubcolumn === "true";
    const isBase = card.dataset.isBase === "true";
    const isGrouped = card.dataset.isGrouped === "true";

    if (chartViewMode === "grouped") {
      card.hidden = isSub || isBase;
    } else {
      card.hidden = isGrouped;
    }
  });

  chartData.forEach((chart, chartIndex) => {
    chart.variants.forEach((variant, variantIndex) => {
      renderChart(chartIndex, variantIndex);
    });
    renderFrequencyTable(chartIndex);
  });

  updateFieldSelectOptions();
  filterCharts();
}

function filterCharts() {
  const query = chartSearch?.value.trim().toLocaleLowerCase() || "";
  const selectedField = chartFieldSelect?.value || "";
  const relatedGroups = [...document.querySelectorAll(".related-chart-group")];
  let visibleCount = 0;
  let totalEligible = 0;
  const hasFilter = Boolean(query || selectedField);

  if (hasFilter && relatedGroupOpenStates.size === 0) {
    relatedGroups.forEach((group) => {
      relatedGroupOpenStates.set(group, group.open);
    });
  }

  chartCards.forEach((card) => {
    const isSub = card.dataset.isSubcolumn === "true";
    const isBase = card.dataset.isBase === "true";
    const isGrouped = card.dataset.isGrouped === "true";

    let isEligible = true;
    if (chartViewMode === "grouped") {
      isEligible = !isSub && !isBase;
    } else {
      isEligible = !isGrouped;
    }

    if (!isEligible) {
      card.hidden = true;
      return;
    }

    totalEligible += 1;
    const name = card.dataset.chartName;
    const matches =
      name.toLocaleLowerCase().includes(query) &&
      (!selectedField || name === selectedField);
    card.hidden = !matches;
    if (matches) visibleCount += 1;
  });

  relatedGroups.forEach((group) => {
    const hasVisibleCards =
      group.querySelector(".chart-card:not([hidden])") !== null;
    group.hidden = !hasVisibleCards;
    if (hasFilter && hasVisibleCards) group.open = true;
  });

  if (!hasFilter && relatedGroupOpenStates.size) {
    relatedGroupOpenStates.forEach((wasOpen, group) => {
      group.open = wasOpen;
    });
    relatedGroupOpenStates.clear();
  }

  if (chartSearchResults) {
    chartSearchResults.textContent = hasFilter
      ? `${visibleCount} of ${totalEligible} graphs`
      : `${totalEligible} graphs`;
  }
}

chartSearch?.addEventListener("input", filterCharts);
chartFieldSelect?.addEventListener("change", filterCharts);

document.querySelectorAll("[data-chart-mode]").forEach((btn) => {
  btn.addEventListener("click", () => {
    chartViewMode = btn.dataset.chartMode;
    document.querySelectorAll("[data-chart-mode]").forEach((b) => {
      b.classList.toggle("active", b === btn);
      b.setAttribute("aria-checked", String(b === btn));
    });
    applyViewMode();
  });
});

document.querySelectorAll("[data-toggle-subcolumns]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const parentName = btn.dataset.toggleSubcolumns;
    const subCards = document.querySelectorAll(
      `.chart-card[data-parent-group="${parentName}"][data-is-subcolumn="true"]`
    );
    const isExpanded = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", String(!isExpanded));
    const count = subCards.length;
    const textNode = btn.querySelector("span");
    if (textNode) {
      textNode.textContent = isExpanded
        ? `View individual columns (${count})`
        : `Hide individual columns (${count})`;
    }

    subCards.forEach((card) => {
      card.hidden = isExpanded;
      if (!isExpanded) {
        card.querySelectorAll("[data-chart-host]").forEach((host) => {
          const parts = host.dataset.chartHost.split("-").map(Number);
          renderChart(parts[0], parts[1]);
        });
      }
    });
  });
});

/* ---------- events and first render ---------- */

document.querySelectorAll("[data-category-filter]").forEach((select) => {
  const chartIndex = Number(select.dataset.categoryFilter);
  categoryFilters.set(chartIndex, "all");
  select.addEventListener("change", () => {
    categoryFilters.set(chartIndex, select.value);
    chartData[chartIndex].variants.forEach((variant, variantIndex) => {
      renderChart(chartIndex, variantIndex);
    });
    renderFrequencyTable(chartIndex);
  });
});

document.getElementById("label-language")?.addEventListener("change", (event) => {
  chartLanguage = event.target.value;
  renderLanguage();
});

updateFieldSelectOptions();
applyViewMode();
renderLanguage();


/* ---------- PID viewer modal ---------- */

function showPidsModal(chartName, categoryLabel, pidsList) {
  const modal = document.getElementById("pid-viewer-modal");
  if (!modal) return;
  const inPids = Array.isArray(pidsList) ? pidsList : [];

  // Compute the excluded set: all dataset PIDs not in inPids
  const inSet = new Set(inPids.map((p) => String(p).trim().toLowerCase()));
  const excludedPids = allDatasetPids.filter(
    (p) => !inSet.has(String(p).trim().toLowerCase())
  );

  let showingExcluded = false;
  let activeList = inPids;

  const subtitle = document.getElementById("pid-modal-subtitle");
  const grid = document.getElementById("pid-chip-grid");
  const countEl = document.getElementById("pid-modal-count");
  const toggleBtn = document.getElementById("pid-modal-toggle");
  const copyBtn = document.getElementById("pid-modal-copy");

  if (subtitle) subtitle.textContent = `${categoryLabel}${chartName ? " \u00b7 " + chartName : ""}`;

  // Reset toggle state
  if (toggleBtn) {
    toggleBtn.dataset.showing = "in";
    toggleBtn.textContent = `Show excluded (${excludedPids.length.toLocaleString()})`;
    toggleBtn.classList.remove("active");
  }

  function makeChip(pid) {
    const chip = document.createElement("span");
    chip.className = "pid-chip pid-chip-clickable";
    chip.textContent = pid;
    chip.title = `Click to navigate to ${pid} in the dataset`;
    chip.setAttribute("role", "button");
    chip.tabIndex = 0;
    const navigate = () => {
      if (typeof window.navigateToPidRow === "function") window.navigateToPidRow(pid);
    };
    chip.addEventListener("click", navigate);
    chip.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); navigate(); }
    });
    return chip;
  }

  function renderChips(list) {
    if (!grid) return;
    grid.replaceChildren();
    list.forEach((pid) => grid.append(makeChip(pid)));
    if (countEl) {
      const total = showingExcluded ? excludedPids.length : inPids.length;
      countEl.textContent = `${list.length.toLocaleString()} of ${total.toLocaleString()} PIDs`;
    }
  }

  renderChips(inPids);
  activeList = inPids;

  // Re-attach search listener each time (clear old ones by cloning)
  const searchEl = document.getElementById("pid-modal-search");
  if (searchEl) {
    const fresh = searchEl.cloneNode(true);
    fresh.value = "";
    searchEl.parentNode.replaceChild(fresh, searchEl);
    fresh.addEventListener("input", () => {
      const q = fresh.value.trim().toLowerCase();
      renderChips(q ? activeList.filter((p) => String(p).toLowerCase().includes(q)) : activeList);
    });
  }

  // Toggle between included / excluded
  if (toggleBtn) {
    const freshToggle = toggleBtn.cloneNode(true);
    toggleBtn.parentNode.replaceChild(freshToggle, toggleBtn);
    freshToggle.textContent = `Show excluded (${excludedPids.length.toLocaleString()})`;
    freshToggle.classList.remove("active");
    freshToggle.dataset.showing = "in";

    freshToggle.addEventListener("click", () => {
      showingExcluded = freshToggle.dataset.showing === "in";
      if (showingExcluded) {
        activeList = excludedPids;
        freshToggle.textContent = `Show in category (${inPids.length.toLocaleString()})`;
        freshToggle.classList.add("active");
        freshToggle.dataset.showing = "excluded";
        if (subtitle) subtitle.textContent = `NOT in: ${categoryLabel}${chartName ? " \u00b7 " + chartName : ""}`;
      } else {
        activeList = inPids;
        freshToggle.textContent = `Show excluded (${excludedPids.length.toLocaleString()})`;
        freshToggle.classList.remove("active");
        freshToggle.dataset.showing = "in";
        if (subtitle) subtitle.textContent = `${categoryLabel}${chartName ? " \u00b7 " + chartName : ""}`;
      }
      // Reset search and render
      const curSearch = document.getElementById("pid-modal-search");
      if (curSearch) curSearch.value = "";
      renderChips(activeList);
    });
  }

  if (copyBtn) {
    copyBtn.onclick = () => {
      navigator.clipboard.writeText(activeList.join("\n")).then(() => {
        const original = copyBtn.textContent;
        copyBtn.textContent = "\u2713 Copied!";
        setTimeout(() => { copyBtn.textContent = original; }, 2000);
      }).catch(() => {});
    };
  }

  modal.showModal();
}

document.getElementById("pid-viewer-close")?.addEventListener("click", () => {
  document.getElementById("pid-viewer-modal")?.close();
});

document.getElementById("pid-viewer-modal")?.addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.close();
});


/* ---------- Chart Snapshot Download & Limit Toggling ---------- */

function downloadChartSnapshot(chartIndex, triggerBtn) {
  const chart = chartData[chartIndex];
  if (!chart) return;
  const host = document.querySelector(`[data-chart-host^="${chartIndex}-"]`);
  const svg = host ? host.querySelector("svg") : null;
  if (!svg) return;

  const originalBtnText = triggerBtn ? triggerBtn.innerHTML : "";
  if (triggerBtn) {
    triggerBtn.textContent = "⏳ Generating...";
    triggerBtn.disabled = true;
  }

  // Clone SVG so we don't modify the on-screen DOM
  const clonedSvg = svg.cloneNode(true);
  clonedSvg.setAttribute("xmlns", svgNamespace);

  // Inject required styles so the SVG self-renders accurately inside canvas
  const styleEl = document.createElementNS(svgNamespace, "style");
  styleEl.textContent = `
    text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
    .chart-gridline { stroke: #e2e8f0; stroke-width: 1px; stroke-dasharray: 4 4; }
    .chart-tick { font-size: 13px; fill: #475569; }
    .chart-value { font-size: 13px; font-weight: 700; fill: #0f172a; }
    .donut-total { font-size: 32px; font-weight: 800; fill: #0f172a; }
    .legend-text { font-size: 13px; fill: #334155; }
    .chart-line { stroke: #7057df; stroke-width: 3.5px; fill: none; }
    .chart-point { fill: #7057df; stroke: #ffffff; stroke-width: 2.5px; }
    .box-whisker { stroke: #7057df; stroke-width: 2px; }
    .box-cap { stroke: #7057df; stroke-width: 2px; }
    .box-range { fill: #ede9fe; stroke: #7057df; stroke-width: 2px; }
    .box-median { stroke: #4338ca; stroke-width: 3px; }
    .box-explanation { font-size: 12px; fill: #64748b; }
    .chart-bar-track { fill: #f1f5f9; }
  `;
  clonedSvg.insertBefore(styleEl, clonedSvg.firstChild);

  const viewBox = clonedSvg.viewBox.baseVal;
  const svgWidth = (viewBox && viewBox.width > 0) ? viewBox.width : 800;
  const svgHeight = (viewBox && viewBox.height > 0) ? viewBox.height : 380;

  const svgXml = new XMLSerializer().serializeToString(clonedSvg);
  const svgBlob = new Blob([svgXml], { type: "image/svg+xml;charset=utf-8" });
  const svgUrl = URL.createObjectURL(svgBlob);

  const img = new Image();
  img.onload = () => {
    try {
      const scale = 2; // 2x Retina resolution
      const headerH = 100;
      const footerH = 45;
      const pad = 24;

      const canvas = document.createElement("canvas");
      canvas.width = (svgWidth + pad * 2) * scale;
      canvas.height = (svgHeight + headerH + footerH + pad) * scale;

      const ctx = canvas.getContext("2d");
      ctx.scale(scale, scale);

      // Clean white background
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, svgWidth + pad * 2, svgHeight + headerH + footerH + pad);

      // Card border
      ctx.strokeStyle = "#e2e8f0";
      ctx.lineWidth = 1.5;
      ctx.strokeRect(6, 6, (svgWidth + pad * 2) - 12, (svgHeight + headerH + footerH + pad) - 12);

      // Category / Eyebrow
      ctx.font = "bold 11px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
      ctx.fillStyle = "#6366f1";
      ctx.fillText("STUDYDASH AUTOMATIC ANALYSIS", pad, pad + 12);

      // Title
      ctx.font = "bold 20px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
      ctx.fillStyle = "#0f172a";
      ctx.fillText(chart.name, pad, pad + 38);

      // Summary
      ctx.font = "13px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
      ctx.fillStyle = "#64748b";
      ctx.fillText(chart.summary || "", pad, pad + 60);

      // Divider line
      ctx.strokeStyle = "#f1f5f9";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(pad, pad + 74);
      ctx.lineTo(svgWidth + pad, pad + 74);
      ctx.stroke();

      // Draw SVG chart with all numbers & bars
      ctx.drawImage(img, pad, headerH, svgWidth, svgHeight);

      // Footer
      const footerY = headerH + svgHeight + 22;
      ctx.font = "11px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
      ctx.fillStyle = "#94a3b8";
      const nowStr = new Date().toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
      ctx.fillText(`StudyDash • Numbers & visual snapshot exported on ${nowStr}`, pad, footerY);

      URL.revokeObjectURL(svgUrl);

      canvas.toBlob((blob) => {
        if (!blob) return;
        const safeName = chart.name.replace(/[^a-zA-Z0-9_\-]/g, "_");
        const a = document.createElement("a");
        a.download = `${safeName}_snapshot.png`;
        a.href = URL.createObjectURL(blob);
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 3000);

        if (triggerBtn) {
          triggerBtn.disabled = false;
          triggerBtn.textContent = "✓ Snapshot Saved!";
          setTimeout(() => { triggerBtn.innerHTML = originalBtnText; }, 2500);
        }
      }, "image/png");
    } catch (err) {
      console.error("Error creating chart snapshot:", err);
      URL.revokeObjectURL(svgUrl);
      if (triggerBtn) {
        triggerBtn.disabled = false;
        triggerBtn.textContent = "Error saving";
        setTimeout(() => { triggerBtn.innerHTML = originalBtnText; }, 2000);
      }
    }
  };

  img.onerror = () => {
    URL.revokeObjectURL(svgUrl);
    if (triggerBtn) {
      triggerBtn.disabled = false;
      triggerBtn.textContent = "Error loading";
      setTimeout(() => { triggerBtn.innerHTML = originalBtnText; }, 2000);
    }
  };

  img.src = svgUrl;
}

document.addEventListener("click", (e) => {
  const toggleBtn = e.target.closest("[data-toggle-chart-limit]");
  if (toggleBtn) {
    const chartIndex = Number(toggleBtn.dataset.toggleChartLimit);
    const current = chartShowAllMap.get(chartIndex) === true;
    chartShowAllMap.set(chartIndex, !current);

    const chart = chartData[chartIndex];
    const total = chart?.variants[0]?.labels?.length || 0;

    renderChart(chartIndex, 0);
    renderFrequencyTable(chartIndex);

    const summaryEl = document.getElementById(`chart-summary-${chartIndex}`);
    if (summaryEl) {
      if (!current) {
        summaryEl.textContent = `Showing all ${total.toLocaleString()} records`;
      } else {
        summaryEl.textContent = `Showing first 10 of ${total.toLocaleString()} records`;
      }
    }
    return;
  }

  const snapBtn = e.target.closest("[data-snapshot-chart]");
  if (snapBtn) {
    const chartIndex = Number(snapBtn.dataset.snapshotChart);
    downloadChartSnapshot(chartIndex, snapBtn);
    return;
  }
});
