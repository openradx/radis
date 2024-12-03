/**
 * An Alpine component that controls the questions formset
 *
 * @param {HTMLElement} rootEl - The element to attach the QuestionsForm to
 * @return {Object} An object with an addQuestion method
 */
function QuestionsForm(rootEl, ) {
  const template = rootEl.querySelector("template");
  const container = rootEl.querySelector("#questions-formset");
  /** @type {HTMLInputElement} */
  const totalForms = rootEl.querySelector("#id_1-TOTAL_FORMS") || rootEl.querySelector("#id_2-TOTAL_FORMS");

  return {
    questionsCount: 1,
    init() {
      this.questionsCount = parseInt(totalForms.value);
      // updateDeleteButtons();
    },
    addQuestion() {
      const newForm = template.content.cloneNode(true);
      const idx = totalForms.value;
      container.append(newForm);
      const lastForm = container.querySelector(".card:last-child");
      lastForm.innerHTML = lastForm.innerHTML.replace(/__prefix__/g, idx);
      totalForms.value = (parseInt(idx) + 1).toString();
      this.questionsCount = parseInt(totalForms.value);
    },
    /**
     * @param {HTMLElement} btnEl
     */
    deleteQuestion(btnEl) {
      btnEl.closest(".card").remove();
      const idx = totalForms.value;
      totalForms.value = (parseInt(idx) - 1).toString();
      this.questionsCount = parseInt(totalForms.value);
    },
  };
}
