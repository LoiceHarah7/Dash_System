const participantsToggle = document.getElementById("participants-toggle");
const participantsListPanel = document.getElementById("participants-list-panel");
const missingPidsToggle = document.getElementById("missing-pids-toggle");
const missingPidRecords = document.getElementById("missing-pid-records");
const allPidsToggle = document.getElementById("all-pids-toggle");
const allPidRecords = document.getElementById("all-pid-records");
const pidGapsToggle = document.getElementById("pid-gaps-toggle");
const pidSequenceGaps = document.getElementById("pid-sequence-gaps");

function togglePidRecords(toggle, section) {
  toggle?.addEventListener("click", () => {
    const isExpanded = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", String(!isExpanded));
    section.hidden = isExpanded;
    if (!isExpanded) {
      section.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });
}

togglePidRecords(participantsToggle, participantsListPanel);
togglePidRecords(missingPidsToggle, missingPidRecords);
togglePidRecords(allPidsToggle, allPidRecords);
togglePidRecords(pidGapsToggle, pidSequenceGaps);

document.querySelectorAll("[data-table-mode]").forEach((button) => {
  button.addEventListener("click", () => {
    const mode = button.dataset.tableMode;
    const container =
      button.closest(".table-view-controls")?.parentElement ||
      button.closest("section, details, .duplicate-section") ||
      document;

    button.parentElement.querySelectorAll("[data-table-mode]").forEach((btn) => {
      btn.classList.toggle("active", btn === button);
      btn.setAttribute("aria-pressed", String(btn === button));
    });

    container.querySelectorAll(".table-wrap, .duplicate-table-wrap").forEach((wrap) => {
      wrap.classList.toggle("view-grouped", mode === "grouped");
      wrap.classList.toggle("view-all", mode === "all");
    });
  });
});

/* ===== Participant Procedure Tracking Interactions ===== */
(function initTracking() {
  const siteTabs = document.querySelectorAll(".tracking-site-tab");
  const downloadBtn = document.getElementById("btn-download-tracking");
  const uploadToggleBtn = document.getElementById("btn-toggle-tracking-upload");
  const uploadDrawer = document.getElementById("tracking-upload-drawer");
  const cancelUploadBtn = document.getElementById("btn-cancel-tracking-upload");
  const siteSelect = document.getElementById("tracking-site-select");

  if (!siteTabs.length) return;

  // Toggle upload drawer
  uploadToggleBtn?.addEventListener("click", () => {
    const isHidden = uploadDrawer.hidden;
    uploadDrawer.hidden = !isHidden;
    uploadToggleBtn.setAttribute("aria-expanded", String(isHidden));
  });

  cancelUploadBtn?.addEventListener("click", () => {
    uploadDrawer.hidden = true;
    uploadToggleBtn?.setAttribute("aria-expanded", "false");
  });

  // Switch site tabs
  siteTabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      const site = tab.dataset.siteTarget; // "lilongwe" or "salima"
      siteTabs.forEach((t) => {
        const isActive = t === tab;
        t.classList.toggle("active", isActive);
        t.setAttribute("aria-selected", String(isActive));
      });

      document.querySelectorAll(".tracking-site-pane").forEach((pane) => {
        pane.hidden = pane.id !== `pane-${site}`;
        pane.classList.toggle("active", pane.id === `pane-${site}`);
      });

      // Update download URL
      if (downloadBtn) {
        const currentHref = downloadBtn.getAttribute("href");
        downloadBtn.setAttribute(
          "href",
          currentHref.replace(/\/(lilongwe|salima)$/i, `/${site}`)
        );
      }

      // Update site selector in upload drawer
      if (siteSelect) {
        siteSelect.value = site === "salima" ? "Salima" : "Lilongwe";
      }
    });
  });

  // Filter rows per site pane
  document.querySelectorAll(".tracking-site-pane").forEach((pane) => {
    const searchInput = pane.querySelector(".tracking-filter-input");
    const filterBtns = pane.querySelectorAll(".tracking-filter-btn");
    const reasonPills = pane.querySelectorAll(".reason-filter-pill");
    const clearPill = pane.querySelector(".reason-clear-pill");
    const rows = pane.querySelectorAll(".tracking-table tbody tr");

    let currentStatus = "all";
    let currentReason = "";
    let currentQuery = "";

    function applyFilter() {
      const q = currentQuery.trim().toLowerCase();
      const reasonFilter = currentReason.trim().toLowerCase();

      rows.forEach((row) => {
        const endo = (row.dataset.endo || "").toLowerCase();
        const comment = (row.dataset.comment || "").toLowerCase();
        const searchBlob = (row.dataset.searchBlob || "").toLowerCase();

        let matchStatus = true;
        if (currentStatus === "yes") {
          matchStatus = endo === "yes";
        } else if (currentStatus === "no") {
          matchStatus = endo === "no";
        } else if (currentStatus === "pending") {
          matchStatus = endo !== "yes" && endo !== "no";
        } else if (currentStatus === "comment") {
          matchStatus = Boolean(comment);
        }

        let matchReason = true;
        if (reasonFilter) {
          matchReason = comment.includes(reasonFilter);
        }

        let matchQuery = true;
        if (q) {
          matchQuery = searchBlob.includes(q);
        }

        row.style.display = matchStatus && matchReason && matchQuery ? "" : "none";
      });
    }

    searchInput?.addEventListener("input", (e) => {
      currentQuery = e.target.value;
      applyFilter();
    });

    filterBtns.forEach((btn) => {
      btn.addEventListener("click", () => {
        filterBtns.forEach((b) => b.classList.toggle("active", b === btn));
        currentStatus = btn.dataset.filter;
        applyFilter();
      });
    });

    reasonPills.forEach((pill) => {
      pill.addEventListener("click", () => {
        const val = pill.dataset.reasonVal;
        if (currentReason === val) {
          currentReason = "";
          pill.classList.remove("active");
          if (clearPill) clearPill.hidden = true;
        } else {
          reasonPills.forEach((p) => p.classList.remove("active"));
          pill.classList.add("active");
          currentReason = val;
          if (clearPill) clearPill.hidden = false;
        }
        applyFilter();
      });
    });

    clearPill?.addEventListener("click", () => {
      currentReason = "";
      reasonPills.forEach((p) => p.classList.remove("active"));
      clearPill.hidden = true;
      applyFilter();
    });
  });
})();

/* ===== Tracking Edit Modal ===== */
(function initTrackingEdit() {
  const modal = document.getElementById("tracking-edit-modal");
  if (!modal) return;
  const form = document.getElementById("tracking-edit-form");
  const pidLabel = document.getElementById("edit-modal-pid");
  const closeBtn = document.getElementById("tracking-edit-close");
  const cancelBtn = document.getElementById("tracking-edit-cancel");

  function openModal(btn) {
    const { id, pid, q, dental, endo, endoDate, comment, action } = btn.dataset;
    if (pidLabel) pidLabel.textContent = pid || "";
    document.getElementById("edit-row-id").value = id || "";
    document.getElementById("edit-q-date").value = (q && q !== "—") ? q : "";
    document.getElementById("edit-dental-date").value = (dental && dental !== "—") ? dental : "";
    const endoSel = document.getElementById("edit-endo-status");
    endoSel.value = (endo === "YES" || endo === "NO") ? endo : "";
    document.getElementById("edit-endo-date").value = (endoDate && endoDate !== "—") ? endoDate : "";
    document.getElementById("edit-comment").value = comment || "";
    if (form && action) form.action = action;
    modal.showModal();
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".edit-tracking-btn");
    if (btn) openModal(btn);
  });

  closeBtn?.addEventListener("click", () => modal.close());
  cancelBtn?.addEventListener("click", () => modal.close());

  // Close on backdrop click
  modal.addEventListener("click", (e) => {
    if (e.target === modal) modal.close();
  });
})();

/* ===== Tracking Section Minimise ===== */
(function initTrackingMinimise() {
  const btn = document.getElementById("btn-minimise-tracking");
  const body = document.getElementById("tracking-body");
  const trackingNavJump = document.getElementById("tracking-nav-jump");
  if (!btn || !body) return;

  function setTrackingState(expanded) {
    btn.setAttribute("aria-expanded", String(expanded));
    body.hidden = !expanded;
    const icon = btn.querySelector(".minimise-icon");
    if (icon) icon.textContent = expanded ? "▲" : "▼";
    const label = btn.querySelector(".minimise-label");
    if (label) {
      label.textContent = expanded ? "Minimise" : "Expand Tracking";
    } else {
      btn.childNodes.forEach((n) => {
        if (n.nodeType === Node.TEXT_NODE) {
          n.textContent = expanded ? " Minimise" : " Expand Tracking";
        }
      });
    }
  }

  btn.addEventListener("click", () => {
    const isExpanded = btn.getAttribute("aria-expanded") === "true";
    setTrackingState(!isExpanded);
  });

  trackingNavJump?.addEventListener("click", () => {
    setTrackingState(true);
  });
})();

/* ===== PID chip → navigate to dataset row ===== */
function navigateToPidRow(pid) {
  if (!pid) return;
  const pidNorm = String(pid).trim().toLowerCase();

  // 1. Try #all-pid-records first (has all rows)
  const allPidSection = document.getElementById("all-pid-records");
  const allPidsToggle = document.getElementById("all-pids-toggle");

  // Expand the section if collapsed
  if (allPidSection && allPidSection.hidden) {
    allPidSection.hidden = false;
    if (allPidsToggle) allPidsToggle.setAttribute("aria-expanded", "true");
  }

  // Find the matching row
  const rows = document.querySelectorAll("[data-dataset-row]");
  let targetRow = null;
  for (const row of rows) {
    const rowPids = (row.dataset.primaryPid || "").trim().toLowerCase();
    const allPids = (row.dataset.pids || "").toLowerCase();
    if (rowPids === pidNorm || allPids.split(/\s+/).includes(pidNorm)) {
      targetRow = row;
      break;
    }
  }

  if (targetRow) {
    // Close the PID modal
    document.getElementById("pid-viewer-modal")?.close();

    // Scroll to and highlight
    targetRow.scrollIntoView({ behavior: "smooth", block: "center" });
    targetRow.classList.add("row-highlight");
    setTimeout(() => targetRow.classList.remove("row-highlight"), 2800);
  } else {
    // PID not in preview — tell user to download full data
    alert(`PID "${pid}" is not in the preview rows. Download the cleaned dataset to see all records.`);
  }
}

// Expose globally so charts.js showPidsModal can call it
window.navigateToPidRow = navigateToPidRow;


/* ===== Data Preview Table Toggle (First 10 vs All) ===== */
(function initPreviewRecordsToggle() {
  const btn = document.getElementById("btn-toggle-preview-records");
  const countLabel = document.getElementById("preview-count-label");
  if (!btn) return;

  btn.addEventListener("click", () => {
    const isExpanded = btn.getAttribute("aria-expanded") === "true";
    const extraRows = document.querySelectorAll(".preview-extra-row");
    const totalCount = extraRows.length + 10;

    btn.setAttribute("aria-expanded", String(!isExpanded));
    extraRows.forEach((row) => {
      row.hidden = isExpanded;
    });

    const span = btn.querySelector("span") || btn;
    if (isExpanded) {
      span.textContent = `Show all records (${totalCount}) ▾`;
      if (countLabel) countLabel.textContent = `First 10 records of ${totalCount} · all rows in downloaded report`;
    } else {
      span.textContent = `Show first 10 records ▴`;
      if (countLabel) countLabel.textContent = `Showing all ${totalCount} records · all rows in downloaded report`;
    }
  });
})();


/* ===== Hide / Unhide Columns Modal (Password-Protected) ===== */
(function initHideColumnsModal() {
  const toggleBtn = document.getElementById("hide-columns-toggle");
  const modal = document.getElementById("hide-columns-modal");
  if (!toggleBtn || !modal) return;

  const closeBtn = document.getElementById("hide-columns-close");
  const authStep = document.getElementById("hide-cols-auth-step");
  const authForm = document.getElementById("hide-cols-auth-form");
  const pwInput = document.getElementById("hide-cols-password");
  const authError = document.getElementById("hide-cols-auth-error");
  const authCancel = document.getElementById("hide-cols-auth-cancel");

  const listStep = document.getElementById("hide-cols-list-step");
  const searchInput = document.getElementById("hide-cols-search");
  const countEl = document.getElementById("hide-cols-count");
  const tbody = document.getElementById("hide-cols-tbody");
  const doneBtn = document.getElementById("hide-cols-done-btn");

  const metaEl = document.getElementById("dataset-meta");
  const datasetId = metaEl ? metaEl.dataset.datasetId : null;

  let verifiedPassword = null;
  let dirty = false;

  function updateCount() {
    if (!tbody || !countEl) return;
    const rows = Array.from(tbody.querySelectorAll(".hide-col-row"));
    const lockedCount = rows.filter((r) => r.dataset.locked === "true").length;
    const visibleRows = rows.filter((r) => !r.hidden).length;
    countEl.textContent = `${visibleRows} shown · ${lockedCount} locked`;
  }

  function openModal() {
    dirty = false;
    if (verifiedPassword) {
      authStep.hidden = true;
      listStep.hidden = false;
      updateCount();
      if (searchInput) searchInput.focus();
    } else {
      authStep.hidden = false;
      listStep.hidden = true;
      if (pwInput) pwInput.value = "";
      if (authError) authError.hidden = true;
    }
    modal.showModal();
    if (!verifiedPassword && pwInput) pwInput.focus();
  }

  function closeModal() {
    modal.close();
    if (dirty) {
      window.location.reload();
    }
  }

  toggleBtn.addEventListener("click", openModal);
  if (closeBtn) closeBtn.addEventListener("click", closeModal);
  if (authCancel) authCancel.addEventListener("click", () => modal.close());
  if (doneBtn) doneBtn.addEventListener("click", closeModal);

  if (authForm) {
    authForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (authError) authError.hidden = true;
      const password = pwInput.value;
      try {
        const resp = await fetch("/api/verify-password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password }),
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
          if (authError) {
            authError.textContent = data.error || "Incorrect password.";
            authError.hidden = false;
          }
          pwInput.select();
          return;
        }
        verifiedPassword = password;
        authStep.hidden = true;
        listStep.hidden = false;
        updateCount();
        if (searchInput) searchInput.focus();
      } catch (err) {
        if (authError) {
          authError.textContent = "Could not verify password. Try again.";
          authError.hidden = false;
        }
      }
    });
  }

  if (searchInput && tbody) {
    searchInput.addEventListener("input", () => {
      const q = searchInput.value.trim().toLowerCase();
      const rows = tbody.querySelectorAll(".hide-col-row");
      rows.forEach((row) => {
        const text = row.textContent.toLowerCase();
        row.hidden = q !== "" && !text.includes(q);
      });
      updateCount();
    });
  }

  if (tbody && datasetId) {
    tbody.addEventListener("click", async (e) => {
      const btn = e.target.closest(".hide-col-toggle-btn");
      if (!btn || !verifiedPassword) return;
      const row = btn.closest(".hide-col-row");
      const column = btn.dataset.column;
      const currentlyLocked = row.dataset.locked === "true";
      const nextLocked = !currentlyLocked;

      btn.disabled = true;
      try {
        const resp = await fetch(`/studies/${datasetId}/hidden-columns`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            password: verifiedPassword,
            column,
            locked: nextLocked,
          }),
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
          alert(data.error || "Could not update column lock state.");
          return;
        }
        dirty = true;
        row.dataset.locked = nextLocked ? "true" : "false";
        row.classList.toggle("is-locked", nextLocked);
        const badge = row.querySelector(".hide-col-badge");
        if (badge) {
          badge.className = `hide-col-badge ${nextLocked ? "badge-locked" : "badge-visible"}`;
          badge.textContent = nextLocked ? "🔒 Locked" : "👁 Visible";
        }
        btn.textContent = nextLocked ? "🔓 Unlock" : "🔒 Lock";
        updateCount();
      } catch (err) {
        alert("Network error while updating column visibility.");
      } finally {
        btn.disabled = false;
      }
    });
  }
})();
