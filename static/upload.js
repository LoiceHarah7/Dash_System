const fileInput = document.getElementById("file");
const firstThreeDisplay = document.getElementById("first-three-cols-display");
const customColsContainer = document.getElementById("custom-columns-container");
const customColsList = document.getElementById("custom-columns-list");
const customColsCount = document.getElementById("custom-cols-count");
const cleanModeRadios = document.querySelectorAll('input[name="clean_mode"]');

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

function renderColumnChoices(columns) {
  if (!columns || !columns.length) return;

  // 1. Update First 3 Columns display
  if (firstThreeDisplay) {
    const first3 = columns.slice(0, 3);
    firstThreeDisplay.innerHTML = `<strong>Columns:</strong> <code>${first3.map(c => escapeHtml(c)).join('</code> + <code>')}</code>`;
    firstThreeDisplay.classList.add("loaded");
  }

  // 2. Populate Custom Columns Checkboxes
  if (customColsList) {
    customColsList.innerHTML = "";
    columns.forEach((col, idx) => {
      const chip = document.createElement("label");
      chip.className = "custom-col-chip";
      
      const chk = document.createElement("input");
      chk.type = "checkbox";
      chk.name = "custom_columns";
      chk.value = col;
      if (idx < 3) chk.checked = true;
      chk.addEventListener("change", updateCustomColsCount);

      const span = document.createElement("span");
      span.textContent = col;

      chip.appendChild(chk);
      chip.appendChild(span);
      customColsList.appendChild(chip);
    });
    updateCustomColsCount();
  }
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
        firstThreeDisplay.textContent = "Select a data file above to preview column names";
        firstThreeDisplay.classList.remove("loaded");
      }
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
          const firstLine = text.split(/\r\n|\n|\r/)[0];
          const sep = lowerName.endsWith(".tsv") || firstLine.includes("\t") ? "\t" : ",";
          const headers = parseSimpleCsvHeaders(firstLine, sep);
          if (headers.length) {
            renderColumnChoices(headers);
          }
        } catch (err) {
          console.warn("Client CSV read error:", err);
        }
      };
      reader.readAsText(file.slice(0, 8192));
    }

    // API inspection for guaranteed fidelity (Excel and CSV)
    const formData = new FormData();
    formData.append("file", file);
    fetch("/api/inspect-headers", { method: "POST", body: formData })
      .then((r) => r.json())
      .then((data) => {
        if (data && data.columns && data.columns.length) {
          renderColumnChoices(data.columns);
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
