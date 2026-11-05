const fileInput = document.getElementById("file");

if (fileInput) {
  const fileName = document.getElementById("file-name");
  const fileDetails = document.getElementById("file-details");

  fileInput.addEventListener("change", () => {
    const [file] = fileInput.files;
    if (!file) {
      fileName.textContent = "Choose a data file to upload";
      fileDetails.textContent = "CSV or Excel · up to 25 MB";
      return;
    }

    const size =
      file.size < 1024 * 1024
        ? `${Math.max(1, Math.round(file.size / 1024))} KB`
        : `${(file.size / (1024 * 1024)).toFixed(1)} MB`;
    fileName.textContent = file.name;
    fileDetails.textContent = `${size} · ready to upload`;
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
