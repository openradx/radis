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
