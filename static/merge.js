/**
 * Merge New Dataset Feature
 * Allows uploading a new Excel dataset, comparing columns side-by-side with
 * visual diff (red for missing), adding/dropping columns, and generating a
 * non-destructive merged copy without modifying originals.
 */

(function () {
  const panel = document.getElementById("merge-panel");
  if (!panel) return;

  const datasetId = panel.dataset.datasetId;
  const openBtn = document.getElementById("merge-open-btn");
  const closeBtn = document.getElementById("merge-close-btn");
  const fileInput = document.getElementById("merge-file-input");
  const filenameLabel = document.getElementById("merge-filename");
  const compareBtn = document.getElementById("merge-compare-btn");
  const errorBox = document.getElementById("merge-error");

  const stepUpload = document.getElementById("merge-step-upload");
  const stepCompare = document.getElementById("merge-step-compare");
  const stepResult = document.getElementById("merge-step-result");

  const compareGrid = document.getElementById("merge-compare-grid");
  const backBtn = document.getElementById("merge-back-btn");
  const executeBtn = document.getElementById("merge-execute-btn");
  const resultBox = document.getElementById("merge-result-box");
  const restartBtn = document.getElementById("merge-restart-btn");

  let selectedFile = null;
  let comparisonData = null;
  // Per-column user decisions:
  // key: column_name -> { action: 'default' | 'add' | 'delete' }
  let decisions = {};

  // Toggle merge panel
  openBtn?.addEventListener("click", () => {
    const isExpanded = openBtn.getAttribute("aria-expanded") === "true";
    openBtn.setAttribute("aria-expanded", String(!isExpanded));
    panel.hidden = isExpanded;
    if (!isExpanded) {
      panel.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });

  closeBtn?.addEventListener("click", () => {
    openBtn.setAttribute("aria-expanded", "false");
    panel.hidden = true;
  });

  // File selection
  fileInput?.addEventListener("change", (e) => {
    const file = e.target.files?.[0];
    if (file) {
      const ext = file.name.substring(file.name.lastIndexOf(".")).toLowerCase();
      if (ext !== ".xlsx" && ext !== ".xls") {
        showError("Please select an Excel file (.xlsx or .xls).");
        selectedFile = null;
        filenameLabel.textContent = "No file chosen";
        compareBtn.disabled = true;
        return;
      }
      selectedFile = file;
      filenameLabel.textContent = file.name;
      compareBtn.disabled = false;
      hideError();
    } else {
      selectedFile = null;
      filenameLabel.textContent = "No file chosen";
      compareBtn.disabled = true;
    }
  });

  function showError(msg) {
    if (errorBox) {
      errorBox.textContent = msg;
      errorBox.hidden = false;
    }
  }

  function hideError() {
    if (errorBox) {
      errorBox.hidden = true;
      errorBox.textContent = "";
    }
  }

  // Step 1 -> Step 2: Compare Columns
  compareBtn?.addEventListener("click", async () => {
    if (!selectedFile) return;
    hideError();
    compareBtn.disabled = true;
    compareBtn.textContent = "Reading & comparing…";

    const formData = new FormData();
    formData.append("file", selectedFile);

    try {
      const resp = await fetch(`/studies/${datasetId}/merge/compare`, {
        method: "POST",
        body: formData,
      });
      const data = await resp.json();
      if (!resp.ok || data.error) {
        showError(data.error || "Failed to compare datasets.");
        compareBtn.disabled = false;
        compareBtn.textContent = "Compare columns →";
        return;
      }

      comparisonData = data;
      decisions = {};
      // Initialize decisions for mismatched columns: default to 'add' so columns aren't lost
      data.columns.forEach((col) => {
        if (!col.match) {
          decisions[col.column] = "default";
        }
      });

      renderComparisonGrid(data);
      stepUpload.hidden = true;
      stepCompare.hidden = false;
      stepResult.hidden = true;
    } catch (err) {
      showError("A network error occurred while reading the file.");
    } finally {
      compareBtn.disabled = false;
      compareBtn.textContent = "Compare columns →";
    }
  });

  // Render the side-by-side comparison grid
  function renderComparisonGrid(data) {
    if (!compareGrid) return;
    compareGrid.innerHTML = "";

    // Table Header
    const table = document.createElement("table");
    table.className = "merge-diff-table";
    table.innerHTML = `
      <thead>
        <tr>
          <th style="width: 45%;">
            <div class="merge-th-title">Current Study Dataset</div>
            <div class="merge-th-meta">${escapeHtml(data.current_name)} (${data.current_rows.toLocaleString()} records)</div>
          </th>
          <th style="width: 10%; text-align: center;">Status</th>
          <th style="width: 45%;">
            <div class="merge-th-title">Imported Excel Dataset</div>
            <div class="merge-th-meta">${escapeHtml(data.new_name)} (${data.new_rows.toLocaleString()} records)</div>
          </th>
        </tr>
      </thead>
      <tbody></tbody>
    `;

    const tbody = table.querySelector("tbody");

    data.columns.forEach((item) => {
      const tr = document.createElement("tr");
      const isMatch = item.match;
      const colName = item.column;
      const decision = decisions[colName] || "default";

      if (isMatch) {
        tr.className = "merge-row-match";
        tr.innerHTML = `
          <td class="merge-col-cell cell-present">
            <span class="merge-col-name">${escapeHtml(colName)}</span>
            <span class="merge-badge badge-present">✓ In current</span>
          </td>
          <td class="merge-status-cell">
            <span class="merge-status-icon match" title="Identical column">✓ Match</span>
          </td>
          <td class="merge-col-cell cell-present">
            <span class="merge-col-name">${escapeHtml(colName)}</span>
            <span class="merge-badge badge-present">✓ In imported</span>
          </td>
        `;
      } else if (item.in_current && !item.in_new) {
        // Present in Current, Missing in Imported
        tr.className = "merge-row-diff";
        const hasValuesBadge = item.has_values_current
          ? `<span class="merge-badge badge-has-data" title="Column contains data values in current dataset">Has data</span>`
          : `<span class="merge-badge badge-empty" title="Column is empty in current dataset">Empty</span>`;

        let rightCellContent = "";
        let leftCellContent = "";

        if (decision === "delete") {
          // User chose to exclude this column from merge
          tr.classList.add("row-excluded");
          leftCellContent = `
            <div class="merge-cell-content">
              <span class="merge-col-name strikethrough">${escapeHtml(colName)}</span>
              ${hasValuesBadge}
              <span class="merge-badge badge-excluded">Excluded from merge</span>
              <button type="button" class="merge-btn-action undo-btn" data-col="${escapeAttr(colName)}" data-action="default">Undo</button>
            </div>
          `;
          rightCellContent = `
            <div class="merge-cell-content muted">
              <span class="merge-missing-text">Excluded from merge</span>
            </div>
          `;
        } else if (decision === "add") {
          // User chose to add empty column to imported side
          tr.classList.add("row-resolved");
          leftCellContent = `
            <div class="merge-cell-content">
              <span class="merge-col-name">${escapeHtml(colName)}</span>
              ${hasValuesBadge}
            </div>
          `;
          rightCellContent = `
            <div class="merge-cell-content">
              <span class="merge-col-name">${escapeHtml(colName)}</span>
              <span class="merge-badge badge-added">✓ Added (empty)</span>
              <button type="button" class="merge-btn-action undo-btn" data-col="${escapeAttr(colName)}" data-action="default">Undo</button>
            </div>
          `;
        } else {
          // Default: Missing on right side (RED)
          leftCellContent = `
            <div class="merge-cell-content">
              <span class="merge-col-name">${escapeHtml(colName)}</span>
              ${hasValuesBadge}
              <button type="button" class="merge-btn-action delete-btn" data-col="${escapeAttr(colName)}" data-action="delete" title="Drop this column from the merged result">
                🗑️ Exclude column
              </button>
            </div>
          `;
          rightCellContent = `
            <div class="merge-cell-content cell-missing-box">
              <span class="merge-missing-badge">✗ Missing in imported file</span>
              <button type="button" class="merge-btn-action add-btn" data-col="${escapeAttr(colName)}" data-action="add" title="Add this column (empty) to imported dataset so it aligns">
                ➕ Add column
              </button>
            </div>
          `;
        }

        tr.innerHTML = `
          <td class="merge-col-cell ${decision === 'delete' ? 'cell-excluded' : 'cell-present'}">${leftCellContent}</td>
          <td class="merge-status-cell">
            <span class="merge-status-icon diff" title="Different between datasets">≠ Diff</span>
          </td>
          <td class="merge-col-cell ${decision === 'add' ? 'cell-resolved' : (decision === 'delete' ? 'cell-excluded' : 'cell-missing')}">${rightCellContent}</td>
        `;
      } else if (!item.in_current && item.in_new) {
        // Missing in Current, Extra in Imported
        tr.className = "merge-row-diff";
        const hasValuesBadge = item.has_values_new
          ? `<span class="merge-badge badge-has-data" title="Column contains data values in imported dataset">Has data</span>`
          : `<span class="merge-badge badge-empty" title="Column is empty in imported dataset">Empty</span>`;

        let leftCellContent = "";
        let rightCellContent = "";

        if (decision === "delete") {
          // User chose to drop this extra column from imported dataset
          tr.classList.add("row-excluded");
          leftCellContent = `
            <div class="merge-cell-content muted">
              <span class="merge-missing-text">Dropped from imported</span>
            </div>
          `;
          rightCellContent = `
            <div class="merge-cell-content">
              <span class="merge-col-name strikethrough">${escapeHtml(colName)}</span>
              ${hasValuesBadge}
              <span class="merge-badge badge-excluded">Dropped</span>
              <button type="button" class="merge-btn-action undo-btn" data-col="${escapeAttr(colName)}" data-action="default">Undo</button>
            </div>
          `;
        } else if (decision === "add") {
          // User chose to add empty column to current side
          tr.classList.add("row-resolved");
          leftCellContent = `
            <div class="merge-cell-content">
              <span class="merge-col-name">${escapeHtml(colName)}</span>
              <span class="merge-badge badge-added">✓ Added (empty)</span>
              <button type="button" class="merge-btn-action undo-btn" data-col="${escapeAttr(colName)}" data-action="default">Undo</button>
            </div>
          `;
          rightCellContent = `
            <div class="merge-cell-content">
              <span class="merge-col-name">${escapeHtml(colName)}</span>
              ${hasValuesBadge}
            </div>
          `;
        } else {
          // Default: Missing on left side (RED), extra on right side
          leftCellContent = `
            <div class="merge-cell-content cell-missing-box">
              <span class="merge-missing-badge">✗ Missing in current dataset</span>
              <button type="button" class="merge-btn-action add-btn" data-col="${escapeAttr(colName)}" data-action="add" title="Add this column (empty) to current dataset so it aligns">
                ➕ Add column
              </button>
            </div>
          `;
          rightCellContent = `
            <div class="merge-cell-content">
              <span class="merge-col-name">${escapeHtml(colName)}</span>
              ${hasValuesBadge}
              <button type="button" class="merge-btn-action delete-btn" data-col="${escapeAttr(colName)}" data-action="delete" title="Drop this extra column from imported dataset">
                🗑️ Delete column
              </button>
            </div>
          `;
        }

        tr.innerHTML = `
          <td class="merge-col-cell ${decision === 'add' ? 'cell-resolved' : (decision === 'delete' ? 'cell-excluded' : 'cell-missing')}">${leftCellContent}</td>
          <td class="merge-status-cell">
            <span class="merge-status-icon diff" title="Different between datasets">≠ Diff</span>
          </td>
          <td class="merge-col-cell ${decision === 'delete' ? 'cell-excluded' : 'cell-present'}">${rightCellContent}</td>
        `;
      }

      tbody.appendChild(tr);
    });

    compareGrid.appendChild(table);

    // Bind action buttons (Add / Delete / Undo)
    compareGrid.querySelectorAll(".merge-btn-action").forEach((btn) => {
      btn.addEventListener("click", () => {
        const col = btn.dataset.col;
        const action = btn.dataset.action;
        decisions[col] = action;
        renderComparisonGrid(comparisonData);
      });
    });
  }

  // Back button (Step 2 -> Step 1)
  backBtn?.addEventListener("click", () => {
    stepCompare.hidden = true;
    stepUpload.hidden = false;
  });

  // Step 2 -> Step 3: Execute Merge
  executeBtn?.addEventListener("click", async () => {
    if (!selectedFile || !comparisonData) return;

    executeBtn.disabled = true;
    executeBtn.textContent = "Merging & saving copies…";

    // Build final column list based on user decisions
    const finalColumns = [];
    const dropFromNew = [];

    comparisonData.columns.forEach((item) => {
      const col = item.column;
      const dec = decisions[col] || "default";

      if (item.match) {
        finalColumns.push(col);
      } else if (item.in_current && !item.in_new) {
        if (dec !== "delete") {
          finalColumns.push(col);
        }
      } else if (!item.in_current && item.in_new) {
        if (dec === "delete") {
          dropFromNew.push(col);
        } else {
          // If added or default included
          finalColumns.push(col);
        }
      }
    });

    const formData = new FormData();
    formData.append("file", selectedFile);
    formData.append("columns", JSON.stringify(finalColumns));
    formData.append("drop_from_new", JSON.stringify(dropFromNew));

    try {
      const resp = await fetch(`/studies/${datasetId}/merge/execute`, {
        method: "POST",
        body: formData,
      });
      const result = await resp.json();
      if (!resp.ok || result.error) {
        alert(result.error || "Failed to execute merge.");
        executeBtn.disabled = false;
        executeBtn.textContent = "Merge datasets →";
        return;
      }

      renderResult(result);
      stepCompare.hidden = true;
      stepResult.hidden = false;
    } catch (err) {
      alert("A network error occurred while executing the merge.");
    } finally {
      executeBtn.disabled = false;
      executeBtn.textContent = "Merge datasets →";
    }
  });

  // Render Step 3 Result Box
  function renderResult(result) {
    if (!resultBox) return;
    resultBox.innerHTML = `
      <div class="merge-success-banner">
        <span class="merge-success-icon">✓</span>
        <div>
          <h4>Datasets merged successfully!</h4>
          <p>Original datasets were <strong>not modified</strong>. Cleaned copies and the merged dataset were created.</p>
        </div>
      </div>

      <div class="merge-stat-cards">
        <div class="merge-stat-card">
          <span class="label">Current records</span>
          <strong>${result.current_rows.toLocaleString()}</strong>
        </div>
        <div class="merge-stat-card">
          <span class="label">Imported records</span>
          <strong>${result.new_rows.toLocaleString()}</strong>
        </div>
        <div class="merge-stat-card primary">
          <span class="label">Total merged records</span>
          <strong>${result.total_rows.toLocaleString()}</strong>
        </div>
      </div>

      <div class="merge-location-box">
        <label class="merge-location-label">📁 Saved Merged File Location:</label>
        <div class="merge-location-input-group">
          <input type="text" class="merge-location-input" readonly value="${escapeAttr(result.output_path)}" id="merge-output-path-field">
          <button type="button" class="button merge-copy-btn" id="merge-copy-path-btn">📋 Copy Location</button>
        </div>
        <small class="merge-location-hint">The workbook contains three sheets: "Current dataset (copy)", "New dataset (copy)", and "Merged".</small>
      </div>
    `;

    const copyBtn = document.getElementById("merge-copy-path-btn");
    const pathField = document.getElementById("merge-output-path-field");
    copyBtn?.addEventListener("click", () => {
      if (pathField) {
        pathField.select();
        navigator.clipboard?.writeText(pathField.value);
        copyBtn.textContent = "✓ Copied!";
        setTimeout(() => {
          copyBtn.textContent = "📋 Copy Location";
        }, 2000);
      }
    });
  }

  // Restart
  restartBtn?.addEventListener("click", () => {
    selectedFile = null;
    comparisonData = null;
    decisions = {};
    if (fileInput) fileInput.value = "";
    if (filenameLabel) filenameLabel.textContent = "No file chosen";
    if (compareBtn) compareBtn.disabled = true;
    stepResult.hidden = true;
    stepCompare.hidden = true;
    stepUpload.hidden = false;
  });

  function escapeHtml(str) {
    if (!str) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function escapeAttr(str) {
    if (!str) return "";
    return String(str).replace(/"/g, "&quot;");
  }
})();

