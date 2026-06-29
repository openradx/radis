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
    init() {},
    addForm() {
      if (!template) {
        return;
      }
      const idx = totalForms.value;
      const html = template.innerHTML.replace(/__prefix__/g, idx);
      container.insertAdjacentHTML("beforeend", html);
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
