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
