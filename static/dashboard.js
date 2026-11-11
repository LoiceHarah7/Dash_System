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
