document.addEventListener("alpine:init", () => {
  Alpine.directive("grow", (el) => {
    el.style.resize = "none";
    el.style.overflow = "hidden";
    el.style.height = "auto";
    document.body.addEventListener("htmx:afterSettle", () => {
      el.style.resize = "none";
      el.style.overflow = "hidden";
      el.style.height = "auto";
    });
    el.addEventListener("input", () => {
      el.style.height = "auto";
      el.style.overflow = "hidden";
      el.style.height = el.scrollHeight + "px";
    });
  });

  Alpine.directive("prompt", (el) => {
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        if (!e.shiftKey) {
          e.preventDefault();
          htmx.trigger(e.currentTarget.form, "submit");
        }
      }
    });
  });

  Alpine.directive("ignore-empty-inputs", (el) => {
    if (el.tagName.toLowerCase() !== "form") {
      throw new Error(
        "submit-form directive can only be used on <form> elements"
      );
    }

    el.addEventListener("submit", (e) => {
      const inputs = el.querySelectorAll("input, select, textarea");
      console.log(inputs);
      inputs.forEach((input) => {
        const value = input.value.trim();
        if (value === "" || input.type === "submit") {
          input.disabled = true;
        }
      });
    });
  });
});

/**
 * An Alpine component that controls a Django FormSet
 *
 * @param {HTMLElement} rootEl - The form element that contains the formset
 * @return {Object} An object with methods to control the formset
 */
function FormSet(rootEl) {
  const template = rootEl.querySelector("template");
  const container = rootEl.querySelector(".formset-forms");
  /** @type {HTMLInputElement} */
  const totalForms = rootEl.querySelector('[id$="TOTAL_FORMS"]');
  /** @type {HTMLInputElement} */
  const minForms = rootEl.querySelector('[id$="MIN_NUM_FORMS"]');
  /** @type {HTMLInputElement} */
  const maxForms = rootEl.querySelector('[id$="MAX_NUM_FORMS"]');

  return {
    formCount: parseInt(totalForms.value),
    minForms: parseInt(minForms.value),
    maxForms: parseInt(maxForms.value),
    init() {
      console.log(this.formCount);
    },
    addForm() {
      const newForm = template.content.cloneNode(true);
      const idx = totalForms.value;
      container.append(newForm);
      const lastForm = container.querySelector(".formset-form:last-child");
      lastForm.innerHTML = lastForm.innerHTML.replace(/__prefix__/g, idx);
      totalForms.value = (parseInt(idx) + 1).toString();
      this.formCount = parseInt(totalForms.value);
    },
    /**
     * @param {HTMLElement} btnEl - The delete button element that was clicked
     */
    removeForm(btnEl) {
      btnEl.closest(".formset-form").remove();
      const idx = totalForms.value;
      totalForms.value = (parseInt(idx) - 1).toString();
      this.formCount = parseInt(totalForms.value);
    },
  };
}

/**
 * Manages the dynamic selection options input for extraction output fields.
 *
 * @param {HTMLElement} rootEl
 * @returns {Object}
 */
function SelectionOptions(rootEl) {
  const hiddenInput = rootEl.querySelector("[data-selection-input]");
  const formContainer =
    rootEl.closest(".formset-form") ?? rootEl.closest("form") ?? rootEl;
  const outputTypeField =
    formContainer.querySelector('select[name$="-output_type"]') ??
    formContainer.querySelector('select[name="output_type"]');

  return {
    options: [],
    maxOptions: 7,
    supportsSelection: false,
    init() {
      this.options = this.parseOptions(hiddenInput?.value);
      this.updateSupports();
      if (outputTypeField) {
        outputTypeField.addEventListener("change", () => {
          const wasSelection = this.supportsSelection;
          this.updateSupports();
          if (!this.supportsSelection) {
            this.options = [];
          } else if (!wasSelection && this.options.length === 0) {
            this.options = this.parseOptions(hiddenInput?.value);
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
  };
}
