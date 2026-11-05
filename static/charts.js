const chartData = JSON.parse(document.getElementById("chart-data").textContent);
const codebookLabels = JSON.parse(
  document.getElementById("label-map-data")?.textContent || "{}",
);
const chartColors = ["#7057df", "#4b91d2", "#35a778", "#e5a43b", "#e16e78", "#42aab0", "#9470c9", "#81934b", "#df8650", "#7181aa"];
const svgNamespace = "http://www.w3.org/2000/svg";
let chartLanguage = "en";
const categoryFilters = new Map();

function element(name, attributes = {}, text = null) {
  const node = document.createElementNS(svgNamespace, name);
  for (const [key, value] of Object.entries(attributes)) {
    node.setAttribute(key, String(value));
  }
  if (text !== null) node.textContent = text;
  return node;
}

function chartSvg(host, label) {
  host.replaceChildren();
  const svg = element("svg", {
    viewBox: "0 0 640 300",
    role: "img",
    "aria-label": label,
    preserveAspectRatio: "xMidYMid meet",
  });
  svg.classList.add("chart-svg");
  host.append(svg);
  return svg;
}

function drawGrid(svg, maximum, left, top, width, height) {
  const ticks = 4;
  for (let index = 0; index <= ticks; index += 1) {
    const value = maximum * (ticks - index) / ticks;
    const y = top + (height * index) / ticks;
    svg.append(
      element("line", { x1: left, y1: y, x2: left + width, y2: y, class: "chart-gridline" }),
      element("text", { x: left - 9, y: y + 4, "text-anchor": "end", class: "chart-tick" }, Math.round(value).toLocaleString()),
    );
  }
}

function drawVerticalBars(svg, labels, values) {
  const left = 58;
  const top = 20;
  const width = 560;
  const height = 218;
  const maximum = Math.max(...values, 1);
  drawGrid(svg, maximum, left, top, width, height);
  const slot = width / Math.max(labels.length, 1);
  values.forEach((value, index) => {
    const barHeight = (value / maximum) * (height - 4);
    const barWidth = Math.min(38, slot * 0.64);
    const x = left + index * slot + (slot - barWidth) / 2;
    const y = top + height - barHeight;
    svg.append(
      element("rect", { x, y, width: barWidth, height: barHeight, rx: 5, class: "chart-bar", fill: chartColors[index % chartColors.length] }),
      element("text", { x: x + barWidth / 2, y: Math.max(y - 6, top + 9), "text-anchor": "middle", class: "chart-value" }, value.toLocaleString()),
      element("text", { x: x + barWidth / 2, y: top + height + 19, "text-anchor": "middle", class: "chart-tick" }, labels[index].length > 12 ? `${labels[index].slice(0, 11)}…` : labels[index]),
    );
  });
}

function drawHorizontalBars(svg, labels, values) {
  const left = 142;
  const top = 14;
  const width = 470;
  const rowHeight = Math.min(27, 218 / Math.max(labels.length, 1));
  const maximum = Math.max(...values, 1);
  values.forEach((value, index) => {
    const y = top + index * rowHeight;
    const barWidth = (value / maximum) * width;
    const label = labels[index].length > 23 ? `${labels[index].slice(0, 22)}…` : labels[index];
    svg.append(
      element("text", { x: left - 11, y: y + rowHeight * 0.67, "text-anchor": "end", class: "chart-tick" }, label),
      element("rect", { x: left, y: y + rowHeight * 0.2, width, height: Math.max(rowHeight * 0.52, 7), rx: 4, class: "chart-bar-track" }),
      element("rect", { x: left, y: y + rowHeight * 0.2, width: barWidth, height: Math.max(rowHeight * 0.52, 7), rx: 4, fill: chartColors[index % chartColors.length] }),
      element("text", { x: Math.min(left + barWidth + 8, 625), y: y + rowHeight * 0.67, class: "chart-value" }, value.toLocaleString()),
    );
  });
}

function drawLine(svg, labels, values) {
  const left = 58;
  const top = 20;
  const width = 560;
  const height = 218;
  const maximum = Math.max(...values, 1);
  drawGrid(svg, maximum, left, top, width, height);
  const points = values.map((value, index) => ({
    x: left + (values.length <= 1 ? width / 2 : (index / (values.length - 1)) * width),
    y: top + height - (value / maximum) * (height - 5),
  }));
  const path = points.map((point, index) => `${index ? "L" : "M"} ${point.x} ${point.y}`).join(" ");
  svg.append(element("path", { d: path, class: "chart-line" }));
  points.forEach((point, index) => {
    svg.append(element("circle", { cx: point.x, cy: point.y, r: 3.5, class: "chart-point" }));
    if (index === 0 || index === points.length - 1 || index % Math.ceil(points.length / 6) === 0) {
      svg.append(element("text", { x: point.x, y: top + height + 19, "text-anchor": "middle", class: "chart-tick" }, labels[index]));
    }
  });
}

function drawBoxPlot(svg, summary) {
  const [minimum, firstQuartile, median, thirdQuartile, maximum] = summary;
  const left = 55;
  const right = 610;
  const y = 137;
  const span = maximum - minimum || 1;
  const position = (value) => left + ((value - minimum) / span) * (right - left);
  const first = position(firstQuartile);
  const third = position(thirdQuartile);
  const middle = position(median);
  svg.append(
    element("line", { x1: left, y1: y, x2: right, y2: y, class: "box-whisker" }),
    element("line", { x1: left, y1: y - 18, x2: left, y2: y + 18, class: "box-cap" }),
    element("line", { x1: right, y1: y - 18, x2: right, y2: y + 18, class: "box-cap" }),
    element("rect", { x: first, y: y - 34, width: Math.max(third - first, 2), height: 68, rx: 6, class: "box-range" }),
    element("line", { x1: middle, y1: y - 34, x2: middle, y2: y + 34, class: "box-median" }),
  );
  [minimum, firstQuartile, median, thirdQuartile, maximum].forEach((value, index) => {
    const x = position(value);
    svg.append(
      element("line", { x1: x, y1: y + 39, x2: x, y2: y + 46, class: "box-cap" }),
      element("text", { x, y: y + 63, "text-anchor": index === 0 ? "start" : index === 4 ? "end" : "middle", class: "chart-tick" }, Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })),
    );
  });
  svg.append(element("text", { x: 320, y: 52, "text-anchor": "middle", class: "box-explanation" }, "Middle box = middle half of values · line = median · whiskers = min and max"));
}

function drawDonut(svg, labels, values) {
  const centerX = 175;
  const centerY = 140;
  const radius = 84;
  const circumference = 2 * Math.PI * radius;
  const total = values.reduce((sum, value) => sum + value, 0) || 1;
  let offset = 0;
  values.forEach((value, index) => {
    const segment = (value / total) * circumference;
    svg.append(element("circle", {
      cx: centerX,
      cy: centerY,
      r: radius,
      fill: "none",
      stroke: chartColors[index % chartColors.length],
      "stroke-width": 34,
      "stroke-dasharray": `${segment} ${circumference - segment}`,
      "stroke-dashoffset": -offset,
      transform: `rotate(-90 ${centerX} ${centerY})`,
    }));
    offset += segment;
  });
  svg.append(
    element("text", { x: centerX, y: centerY - 2, "text-anchor": "middle", class: "donut-total" }, total.toLocaleString()),
    element("text", { x: centerX, y: centerY + 18, "text-anchor": "middle", class: "chart-tick" }, "records"),
  );
  labels.forEach((label, index) => {
    const y = 54 + index * 25;
    svg.append(
      element("circle", { cx: 330, cy: y - 4, r: 5, fill: chartColors[index % chartColors.length] }),
      element("text", { x: 343, y, class: "chart-tick" }, `${label.length > 20 ? `${label.slice(0, 19)}…` : label} · ${values[index].toLocaleString()}`),
    );
  });
}

function drawPie(svg, labels, values) {
  const centerX = 190;
  const centerY = 140;
  const radius = 94;
  const total = values.reduce((sum, value) => sum + value, 0) || 1;
  if (values.length === 1) {
    svg.append(element("circle", {
      cx: centerX,
      cy: centerY,
      r: radius,
      fill: chartColors[0],
    }));
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
    svg.append(element("path", {
      d: path,
      fill: chartColors[index % chartColors.length],
      stroke: "white",
      "stroke-width": 2,
    }));
    angle = nextAngle;
  });
  labels.forEach((label, index) => {
    const y = 54 + index * 25;
    svg.append(
      element("circle", { cx: 350, cy: y - 4, r: 5, fill: chartColors[index % chartColors.length] }),
      element("text", { x: 363, y, class: "chart-tick" }, `${label.length > 19 ? `${label.slice(0, 18)}…` : label} · ${values[index].toLocaleString()}`),
    );
  });
}

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
  return result;
}

function renderChart(chartIndex, variantIndex) {
  const chart = chartData[chartIndex];
  const filterIndex = categoryFilters.get(chartIndex);
  const variant = filteredVariant(chart.variants[variantIndex], filterIndex);
  variant.labels = localizedLabels(variant);
  const host = document.querySelector(
    `[data-chart-host="${chartIndex}-${variantIndex}"]`,
  );
  const svg = chartSvg(host, `${chart.name}, ${variant.label}`);

  if (
    variant.type === "histogram" ||
    (variant.type === "bar" && chart.kind === "date")
  ) {
    drawVerticalBars(svg, variant.labels, variant.values);
  } else if (variant.type === "line") {
    drawLine(svg, variant.labels, variant.values);
  } else if (variant.type === "box") {
    drawBoxPlot(svg, variant.summary);
  } else if (variant.type === "pie") {
    drawPie(svg, variant.labels, variant.values);
  } else if (variant.type === "donut") {
    drawDonut(svg, variant.labels, variant.values);
  } else {
    drawHorizontalBars(svg, variant.labels, variant.values);
  }
}

function renderFrequencyTable(chartIndex) {
  const chart = chartData[chartIndex];
  if (!chart || chart.kind !== "categories" || !chart.variants.length) return;
  const variant = filteredVariant(
    chart.variants[0],
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
    countCell.textContent = variant.values[index].toLocaleString();
    row.append(labelCell, countCell);
    body.append(row);
  });
  table.append(head, body);
  host.replaceChildren(table);
}

function renderLanguage() {
  chartData.forEach((chart, chartIndex) => {
    chart.variants.forEach((variant, variantIndex) => {
      renderChart(chartIndex, variantIndex);
    });
    renderFrequencyTable(chartIndex);
  });
  document.querySelectorAll("[data-label-column]").forEach((cell) => {
    const code = String(cell.dataset.rawValue);
    const labelsByCode = codebookLabels[cell.dataset.labelColumn] || {};
    const labels = labelsByCode[code] || labelsByCode[normalizeCodeForBrowser(code)];
    cell.textContent =
      labels?.[chartLanguage] || labels?.en || labels?.ny || code;
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

function normalizeCodeForBrowser(value) {
  const number = Number(value);
  return Number.isFinite(number) ? String(number) : String(value);
}

const chartSearch = document.getElementById("chart-search-input");
const chartFieldSelect = document.getElementById("chart-field-select");
const chartSearchResults = document.getElementById("chart-search-results");
const relatedGroupOpenStates = new Map();

const chartCards = [...document.querySelectorAll(".chart-card[data-chart-name]")];
if (chartFieldSelect) {
  chartCards.forEach((card) => {
    const option = document.createElement("option");
    option.value = card.dataset.chartName;
    option.textContent = card.dataset.chartName;
    chartFieldSelect.append(option);
  });
}

function filterCharts() {
  const query = chartSearch?.value.trim().toLocaleLowerCase() || "";
  const selectedField = chartFieldSelect?.value || "";
  const cards = [...document.querySelectorAll(".chart-card[data-chart-name]")];
  const relatedGroups = [...document.querySelectorAll(".related-chart-group")];
  let visibleCount = 0;
  const hasFilter = Boolean(query || selectedField);

  if (hasFilter && relatedGroupOpenStates.size === 0) {
    relatedGroups.forEach((group) => {
      relatedGroupOpenStates.set(group, group.open);
    });
  }

  cards.forEach((card) => {
    const name = card.dataset.chartName;
    const matches =
      name.toLocaleLowerCase().includes(query) &&
      (!selectedField || name === selectedField);
    card.hidden = !matches;
    if (matches) visibleCount += 1;
  });

  relatedGroups.forEach((group) => {
    const hasVisibleCards = group.querySelector(".chart-card:not([hidden])") !== null;
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
      ? `${visibleCount} of ${cards.length} graphs`
      : `${cards.length} graphs`;
  }
}

chartSearch?.addEventListener("input", filterCharts);
chartFieldSelect?.addEventListener("change", filterCharts);

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

chartData.forEach((chart, chartIndex) => {
  chart.variants.forEach((variant, variantIndex) => {
    renderChart(chartIndex, variantIndex);
  });
  renderFrequencyTable(chartIndex);
});
renderLanguage();
