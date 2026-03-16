/**
 * Manages the dynamic selection options input for extraction output fields.
 *
 * @param {HTMLElement} rootEl
 * @returns {Object}
 */
function SelectionOptions(rootEl) {
  const hiddenInput = rootEl.querySelector("[data-selection-input]");
  const arrayInput = rootEl.querySelector("[data-array-input]");
  const formContainer =
    rootEl.closest(".formset-form") ?? rootEl.closest("form") ?? rootEl;
  const outputTypeField =
    formContainer.querySelector('select[name$="-output_type"]') ??
    formContainer.querySelector('select[name="output_type"]');
  const arrayToggleButton =
    formContainer.querySelector("[data-array-toggle]") ?? null;
  const parseArrayValue = (value) => {
    if (!value) {
      return false;
    }
    const normalized = value.trim().toLowerCase();
    return normalized === "true" || normalized === "1" || normalized === "on";
  };
  const parseMaxOptions = () => {
    const datasetValue =
      hiddenInput?.dataset.maxSelectionOptions ??
      rootEl.dataset.maxSelectionOptions ??
      "";
    const parsed = Number.parseInt(datasetValue, 10);
    return Number.isNaN(parsed) ? 0 : parsed;
  };
  const initialMaxOptions = parseMaxOptions();

  return {
    options: [],
    maxOptions: initialMaxOptions,
    supportsSelection: false,
    isArray: parseArrayValue(arrayInput?.value),
    lastSelectionOptions: [],
    init() {
      this.options = this.parseOptions(hiddenInput?.value);
      this.isArray = parseArrayValue(arrayInput?.value);
      this.lastSelectionOptions = [...this.options];
      this.updateSupports();
      if (arrayToggleButton) {
        arrayToggleButton.addEventListener("click", (event) => {
          event.preventDefault();
          this.toggleArray();
        });
        this.updateArrayButton();
      }
      if (outputTypeField) {
        outputTypeField.addEventListener("change", () => {
          const wasSelection = this.supportsSelection;
          this.updateSupports();
          if (!this.supportsSelection) {
            this.lastSelectionOptions = [...this.options];
            this.options = [];
          } else if (!wasSelection && this.options.length === 0) {
            if (this.lastSelectionOptions.length > 0) {
              this.options = [...this.lastSelectionOptions];
            } else {
              this.options = this.parseOptions(hiddenInput?.value);
            }
          }
        });
      }
    },
    parseOptions(value) {
      if (!value) {
        return [];
      }
      try {
        const parsed = JSON.parse(value);
        if (Array.isArray(parsed)) {
          return parsed
            .map((opt) => (typeof opt === "string" ? opt : ""))
            .filter((opt) => opt !== "");
        }
      } catch (err) {
        console.warn("Invalid selection options payload", err);
      }
      return [];
    },
    updateSupports() {
      this.supportsSelection = outputTypeField
        ? outputTypeField.value === "S"
        : false;
    },
    syncOptions() {
      if (!hiddenInput) {
        return;
      }
      const sanitized = this.options
        .map((opt) => (typeof opt === "string" ? opt.trim() : ""))
        .filter((opt) => opt !== "");
      hiddenInput.value = JSON.stringify(sanitized);
      this.lastSelectionOptions = [...sanitized];
    },
    syncArray() {
      if (!arrayInput) {
        return;
      }
      arrayInput.value = this.isArray ? "true" : "false";
    },
    syncState() {
      this.syncOptions();
      this.syncArray();
      this.updateArrayButton();
    },
    addOption() {
      if (!this.supportsSelection || this.options.length >= this.maxOptions) {
        return;
      }
      this.options.push("");
      this.$nextTick(() => {
        const inputs = rootEl.querySelectorAll("[data-selection-option-input]");
        const lastInput = inputs[inputs.length - 1];
        if (lastInput instanceof HTMLInputElement) {
          lastInput.focus();
        }
      });
    },
    removeOption(index) {
      this.options.splice(index, 1);
    },
    toggleArray() {
      this.isArray = !this.isArray;
    },
    updateArrayButton() {
      if (!arrayToggleButton) {
        return;
      }
      arrayToggleButton.classList.toggle("active", this.isArray);
      arrayToggleButton.setAttribute(
        "aria-pressed",
        this.isArray ? "true" : "false"
      );
    },
  };
}
