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

// Prevent form submission on Enter keypress for forms with data-prevent-enter-submit.
// This was added to the Extractions Output Fields Page to prevent form submission
// on pressing Enter while adding Output Fields.
document.addEventListener("DOMContentLoaded", () => {
  const preventAttr = "[data-prevent-enter-submit]";
  document.querySelectorAll(preventAttr).forEach((formEl) => {
    formEl.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") {
        return;
      }
      const target = event.target;
      const isTextInput =
        target instanceof HTMLInputElement &&
        !["submit", "button"].includes(target.type);
      if (isTextInput) {
        event.preventDefault();
      }
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
 *  An Alpine component that controls a Django FormSet.
 * The core idea is to create a small state machine that manages --
 * 1. A list of "Selection Option" strings (like an enum)
 * 2. Whether the current output field supports selections
 * 3. Whether the field should return multiple values (isArray).
 * 4. A snapshot of the last valid selections so we can restore them if the user
 * toggles output types (lastSelectionOptions).
 * 5. Keeping the hidden inputs (JSON representations of users dynamic input)
 * synced so Django can read the state when the form submits.
 *
 * @param {HTMLElement} rootEl
 * @returns {Object}
 */
function SelectionOptions(rootEl) {
  /*data-selection-input and data-array-input are data attributes that are used to 
  locate the the hidden fields that store the JSON serialized state.*/
  const hiddenInput = rootEl.querySelector("[data-selection-input]");
  const arrayInput = rootEl.querySelector("[data-array-input]");

  /*finds the closest wrapper that contains the output-type dropdown and the toggle button */
  const formContainer =
    rootEl.closest(".formset-form") ?? rootEl.closest("form") ?? rootEl;

  /*We are searching for the <select> element that controls the output type 
    (e.g., Text, Numeric, Boolean, Selection). The name attribute of that 
    <select> differs based on whether we are in a formset or a single form,
    so we try both patterns here - this is important in case we want to use this component 
    outside of a formset in the future. */
  const outputTypeField =
    formContainer.querySelector('select[name$="-output_type"]') ??
    formContainer.querySelector('select[name="output_type"]');
  const arrayToggleButton =
    formContainer.querySelector("[data-array-toggle]") ?? null;

  /*Takes the string from the hidden is_array field and turns it into a boolean.*/
  const parseArrayValue = (value) => {
    if (!value) {
      return false;
    }
    const normalized = value.trim().toLowerCase();
    return normalized === "true" || normalized === "1" || normalized === "on";
  };
  const maxSelectionOptions = outputTypeField?.dataset.maxSelectionOptions;

  /*Reads the maximum number of selections allowed. 
  Each form field embeds this limit via data-max-selection-options
  which is set in the initialization of OutputFieldForm */
  const parseMaxOptions = () => {
    const datasetValue =
      hiddenInput?.dataset.maxSelectionOptions ??
      rootEl.dataset.maxSelectionOptions ??
      "";
    const parsed = Number.parseInt(datasetValue, 10);
    return Number.isNaN(parsed) ? 0 : parsed;
  };
  const initialMaxOptions = parseMaxOptions();

  //This represents the "reactive state" (summary of users dynamic input) of the component.
  return {
    options: [], //array of current selection strings displayed in the widget.
    maxOptions: initialMaxOptions,
    supportsSelection: false, //whether the current output type supports selection options.
    isArray: parseArrayValue(arrayInput?.value), //current "array toggle" state,
    // parsed from the hidden field.
    lastSelectionOptions: [], // used to remember the user’s typed options if they
    // switch away from Selection so we can restore them later.

    init() {
      //Populate options by reading the hidden JSON string (if any).
      this.options = this.parseOptions(hiddenInput?.value);
      //isArray based on the hidden is_array value.
      this.isArray = parseArrayValue(arrayInput?.value);
      //Remember the initial options in lastSelectionOptions.
      this.lastSelectionOptions = [...this.options];
      this.updateSupports(); //Determine if current output type supports selection.

      //Clicking the button flips isArray
      if (arrayToggleButton) {
        arrayToggleButton.addEventListener("click", (event) => {
          event.preventDefault();
          this.toggleArray();
        });
        this.updateArrayButton();
      }
      if (outputTypeField) {
        outputTypeField.addEventListener("change", () => {
          /**
           * When the user switches from Selection to another type, we save the current
           * options in lastSelectionOptions and clear options and clear the UI
           * (since we shouldn’t show them for Text/Numeric/Boolean).
           *
           * If they switch back to Selection and there are no options yet,
           *  we restore the previous list (or reload from the hidden JSON).
           *
           * This prevents data loss when toggling between types.
           */
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

    /* Converts the hidden field’s JSON string into a clean array of strings. 
    Non-string entries become empty strings and get filtered out.*/
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

    //Checks the dropdown’s current value. If the field is set to S (Selection),
    // we show the options list. Otherwise, hide it..
    updateSupports() {
      this.supportsSelection = outputTypeField
        ? outputTypeField.value === "S"
        : false;
    },

    /*
    Whenever the options array changes, we trim entries, remove empty strings, serialize to JSON, 
    and store it back in the hidden input. 
    We also update lastSelectionOptions so we remember the sanitized state.
    */
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

    //Writes "true" or "false" into the hidden is_array input.
    syncArray() {
      if (!arrayInput) {
        return;
      }
      arrayInput.value = this.isArray ? "true" : "false";
    },

    /*
      One method to sync everything. 
      We call this via x-effect="syncState()" in the template, 
      so Alpine runs it after every reactive update
    */
    syncState() {
      this.syncOptions();
      this.syncArray();
      this.updateArrayButton();
    },

    //Adds a new empty option if the field is in Selection mode and hasn’t hit the max count.
    addOption() {
      if (!this.supportsSelection || this.options.length >= this.maxOptions) {
        return;
      }
      this.options.push("");
      this.$nextTick(() => {
        // After adding, it waits for the DOM to update ($nextTick) and automatically shifts cursor focus
        // to the newly added input field.
        const inputs = rootEl.querySelectorAll("[data-selection-option-input]");
        const lastInput = inputs[inputs.length - 1];
        if (lastInput instanceof HTMLInputElement) {
          lastInput.focus();
        }
      });
    },

    //Deletes the option at the selected index.
    removeOption(index) {
      this.options.splice(index, 1);
    },

    //Flips the boolean; syncState() will update the hidden input and button appearance.
    toggleArray() {
      this.isArray = !this.isArray;
    },

    //Updates the toggle button’s appearance based on isArray state -
    // we want to show a blue highlight around the button when it is pressed (shows active state).
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
