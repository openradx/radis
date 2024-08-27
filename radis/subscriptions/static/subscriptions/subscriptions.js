/**
 * An Alpine component that controls the questions formset
 *
 * @param {HTMLElement} rootEl - The element to attach the QuestionsForm to
 * @return {Object} An object with an addQuestion method
 */
function QuestionsForm(rootEl) {
    const template = rootEl.querySelector("template");
    const container = rootEl.querySelector("#questions-formset");
    /** @type {HTMLInputElement} */
    const totalForms = rootEl.querySelector("#id_questions-TOTAL_FORMS");

    return {
      questionsCount: 0,
      deletedForms: 0,
      reduceTotalForms() {
        totalForms.value = (parseInt(totalForms.value) - 1).toString();
      },
      increaseTotalForms() {
        totalForms.value = (parseInt(totalForms.value) + 1).toString();
      },
      getTotalForms() {
        return parseInt(totalForms.value);
      },
      init() {
        this.questionsCount = this.getTotalForms();
      },
      addQuestion() {
        const newForm = template.content.cloneNode(true);
        const idx = this.getTotalForms();
        container.append(newForm);
        const lastForm = container.querySelector(".card:last-child");
        lastForm.innerHTML = lastForm.innerHTML.replace(/__prefix__/g, idx.toString());
        this.increaseTotalForms();
        this.questionsCount = this.getTotalForms() - this.deletedForms;
      },
      /**
       * @param {HTMLElement} btnEl
       */
      deleteQuestion(btnEl) {
        const formElement = btnEl.closest('.card');
        const formIdentifier = formElement.querySelector('input[name^="questions-"]').name.match(/questions-(\d+)-/)[1];
        const deleteField = container.querySelector(`input[name="questions-${formIdentifier}-DELETE"]`);

        deleteField.value = 'on';
        formElement.style.display = 'none';
        this.deletedForms++;

        this.questionsCount = this.getTotalForms() - this.deletedForms;
      },
    };
  }
  