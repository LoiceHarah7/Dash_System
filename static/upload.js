const fileInput = document.getElementById("file");
const firstThreeDisplay = document.getElementById("first-three-cols-display");
const firstThreeRow = document.getElementById("first-three-cols-row");
const firstCol1 = document.getElementById("first-col-1");
const firstCol2 = document.getElementById("first-col-2");
const firstCol3 = document.getElementById("first-col-3");
const customColsContainer = document.getElementById("custom-columns-container");
const customColsList = document.getElementById("custom-columns-list");
const customColsCount = document.getElementById("custom-cols-count");
const customColsSearchInput = document.getElementById("custom-cols-search-input");
const btnExpandCustomCols = document.getElementById("btn-expand-custom-cols");
const customColsExpandLabel = document.getElementById("custom-cols-expand-label");
const customColsViewCount = document.getElementById("custom-cols-view-count");
const customColsFooter = document.getElementById("custom-cols-footer");
const cleanModeRadios = document.querySelectorAll('input[name="clean_mode"]');

let allLoadedColumns = [];
let isCustomColsExpanded = false;

function parseSimpleCsvHeaders(firstLine, sep) {
  const result = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < firstLine.length; i++) {
    const char = firstLine[i];
    if (char === '"') {
      inQuotes = !inQuotes;
    } else if (char === sep && !inQuotes) {
      result.push(current.trim().replace(/^"|"$/g, ""));
      current = "";
    } else {
      current += char;
    }
  }
  if (current) {
    result.push(current.trim().replace(/^"|"$/g, ""));
  }
  return result.filter(Boolean);
}

function updateCustomColsCount() {
  if (!customColsList || !customColsCount) return;
  const checked = customColsList.querySelectorAll('input[name="custom_columns"]:checked');
  customColsCount.textContent = `${checked.length} selected`;
}

function applyCustomColsFilter() {
  if (!customColsList) return;
  const query = (customColsSearchInput?.value || "").trim().toLowerCase();
  const chips = customColsList.querySelectorAll(".custom-col-chip");
  let matchCount = 0;

  chips.forEach((chip) => {
    const colName = chip.dataset.colName?.toLowerCase() || "";
    const isMatch = !query || colName.includes(query);
    const index = parseInt(chip.dataset.colIndex, 10);

    if (!isMatch) {
      chip.style.display = "none";
    } else {
      matchCount++;
      if (query) {
        // In search mode, show all matches
        chip.style.display = "inline-flex";
      } else {
        // In browse mode, respect pagination (first 10 vs all)
        if (isCustomColsExpanded || index < 10) {
          chip.style.display = "inline-flex";
        } else {
          chip.style.display = "none";
        }
      }
    }
  });

  if (customColsViewCount) {
    if (query) {
      customColsViewCount.textContent = `Found ${matchCount} of ${allLoadedColumns.length} columns`;
    } else if (isCustomColsExpanded) {
      customColsViewCount.textContent = `Showing all ${allLoadedColumns.length} columns`;
    } else {
      const shown = Math.min(10, allLoadedColumns.length);
      customColsViewCount.textContent = `Showing first ${shown} of ${allLoadedColumns.length} columns`;
    }
  }
}

function renderColumnChoices(columns, displayNames) {
  if (!columns || !columns.length) return;
  allLoadedColumns = columns;
  // displayNames: human-readable labels for the UI; fall back to columns
  const labels = displayNames && displayNames.length === columns.length
    ? displayNames
    : columns;

  // 1. Update First 3 Columns Row Style
  const first3Labels = labels.slice(0, 3);
  if (firstCol1) firstCol1.textContent = first3Labels[0] || "Column 1";
  if (firstCol2) firstCol2.textContent = first3Labels[1] || "Column 2";
  if (firstCol3) firstCol3.textContent = first3Labels[2] || "Column 3";

  if (firstThreeRow) {
    firstThreeRow.classList.add("active-row");
  }
  if (firstThreeDisplay) {
    firstThreeDisplay.textContent = `First 3 columns detected from ${fileInput?.files?.[0]?.name || "file"}`;
    firstThreeDisplay.classList.add("loaded");
  }

  // 2. Populate Custom Columns with Selectable Check Icon
  if (customColsList) {
    customColsList.innerHTML = "";
    isCustomColsExpanded = false;

    columns.forEach((col, idx) => {
      const chip = document.createElement("label");
      chip.className = "custom-col-chip";
      chip.dataset.colName = labels[idx] || col;
      chip.dataset.colIndex = idx;
      if (idx >= 10) {
        chip.classList.add("custom-col-extra");
        chip.style.display = "none";
      } else {
        chip.style.display = "inline-flex";
      }

      const chk = document.createElement("input");
      chk.type = "checkbox";
      chk.name = "custom_columns";
      chk.value = col;
      if (idx < 3) chk.checked = true;
      chk.addEventListener("change", updateCustomColsCount);

      const checkIcon = document.createElement("span");
      checkIcon.className = "chip-check-icon";
      checkIcon.innerHTML = "✓";

      const nameSpan = document.createElement("span");
      nameSpan.className = "chip-name";
      nameSpan.textContent = labels[idx] || col;

      chip.appendChild(chk);
      chip.appendChild(checkIcon);
      chip.appendChild(nameSpan);
      customColsList.appendChild(chip);
    });

    updateCustomColsCount();

    // Setup expand button & footer
    if (customColsFooter) {
      customColsFooter.style.display = columns.length > 10 ? "flex" : "none";
    }
    if (btnExpandCustomCols) {
      btnExpandCustomCols.hidden = columns.length <= 10;
      if (customColsExpandLabel) {
        customColsExpandLabel.textContent = `Show all ${columns.length} columns ▾`;
      }
    }
    if (customColsViewCount) {
      customColsViewCount.textContent = `Showing first ${Math.min(10, columns.length)} of ${columns.length} columns`;
    }
  }
}

// Expand/Collapse Custom Columns
if (btnExpandCustomCols) {
  btnExpandCustomCols.addEventListener("click", () => {
    isCustomColsExpanded = !isCustomColsExpanded;
    if (customColsExpandLabel) {
      customColsExpandLabel.textContent = isCustomColsExpanded
        ? "Show first 10 columns ▴"
        : `Show all ${allLoadedColumns.length} columns ▾`;
    }
    applyCustomColsFilter();
  });
}

// Search Filter Input
if (customColsSearchInput) {
  customColsSearchInput.addEventListener("input", () => {
    applyCustomColsFilter();
  });
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function updateCleanModeUI() {
  const selected = document.querySelector('input[name="clean_mode"]:checked')?.value;
  if (customColsContainer) {
    customColsContainer.style.display = selected === "custom" ? "block" : "none";
  }
}

cleanModeRadios.forEach((radio) => {
  radio.addEventListener("change", updateCleanModeUI);
});

if (fileInput) {
  const fileName = document.getElementById("file-name");
  const fileDetails = document.getElementById("file-details");

  fileInput.addEventListener("change", () => {
    const [file] = fileInput.files;
    if (!file) {
      fileName.textContent = "Choose a data file to upload";
      fileDetails.textContent = "CSV or Excel · up to 25 MB";
      if (firstThreeDisplay) {
        firstThreeDisplay.textContent = "Upload a file above to load column names";
        firstThreeDisplay.classList.remove("loaded");
      }
      if (firstCol1) firstCol1.textContent = "Column 1";
      if (firstCol2) firstCol2.textContent = "Column 2";
      if (firstCol3) firstCol3.textContent = "Column 3";
      if (firstThreeRow) firstThreeRow.classList.remove("active-row");
      return;
    }

    const size =
      file.size < 1024 * 1024
        ? `${Math.max(1, Math.round(file.size / 1024))} KB`
        : `${(file.size / (1024 * 1024)).toFixed(1)} MB`;
    fileName.textContent = file.name;
    fileDetails.textContent = `${size} · ready to upload`;

    if (firstThreeDisplay) {
      firstThreeDisplay.textContent = "Detecting columns...";
    }

    // Fast client-side read for CSV files
    const lowerName = file.name.toLowerCase();
    if ((lowerName.endsWith(".csv") || lowerName.endsWith(".tsv")) && typeof FileReader !== "undefined") {
      const reader = new FileReader();
      reader.onload = (e) => {
        try {
          const text = e.target.result;
          // Find the first newline that is NOT inside a quoted field.
          // CSV headers with embedded newlines (e.g. bilingual RedCap
          // exports) would otherwise break the naive split.
          let inQuotes = false;
          let nlPos = -1;
          for (let i = 0; i < text.length; i++) {
            const ch = text[i];
            if (ch === '"') {
              inQuotes = !inQuotes;
            } else if (!inQuotes && (ch === '\n' || ch === '\r')) {
              nlPos = i;
              break;
            }
          }
          const firstLine = nlPos >= 0 ? text.substring(0, nlPos) : text;
          let sep = ",";
          if (lowerName.endsWith(".tsv")) {
            sep = "\t";
          } else if (lowerName.endsWith(".csv")) {
            sep = firstLine.includes(",") ? "," : (firstLine.includes("\t") ? "\t" : ",");
          } else {
            const commaCount = (firstLine.match(/,/g) || []).length;
            const tabCount = (firstLine.match(/\t/g) || []).length;
            sep = tabCount > commaCount ? "\t" : ",";
          }
          const headers = parseSimpleCsvHeaders(firstLine, sep);
          if (headers.length) {
            renderColumnChoices(headers);
          }
        } catch (err) {
          console.warn("Client CSV read error:", err);
        }
      };
      // Read up to 128 KB to cover CSVs with many columns / long labels
      reader.readAsText(file.slice(0, 131072));
    }

    // API inspection for guaranteed fidelity (Excel and CSV)
    const formData = new FormData();
    formData.append("file", file);
    fetch("/api/inspect-headers", { method: "POST", body: formData })
      .then((r) => r.json())
      .then((data) => {
        if (data && data.columns && data.columns.length) {
          renderColumnChoices(data.columns, data.display_names);
        }
      })
      .catch((err) => {
        console.warn("Could not inspect headers via server:", err);
      });
  });
}

const codebookInput = document.getElementById("codebook");

if (codebookInput) {
  const codebookDetails = document.getElementById("codebook-details");

  codebookInput.addEventListener("change", () => {
    const [file] = codebookInput.files;
    codebookDetails.textContent = file
      ? `${file.name} · ready to upload`
      : "CSV or Excel · common code, value, variable, and label columns supported";
  });
}

const dictionaryInput = document.getElementById("dictionary");

if (dictionaryInput) {
  const dictionaryDetails = document.getElementById("dictionary-details");

  dictionaryInput.addEventListener("change", () => {
    const [file] = dictionaryInput.files;
    dictionaryDetails.textContent = file
      ? `${file.name} · ready to upload`
      : "RedCap data dictionary · maps variables to CRF forms (Demographics, Medical History, …)";
  });
}