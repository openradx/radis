function SearchForm($el) {
  return {
    handleSubmit() {
      // Additionally remove age from and age till form the query url if
      // they are equal to the min and max values of the age range
      // Other fields are handled by the x-ignore-empty-inputs directive
      const ageFromEl = $el.querySelector("#id_age_from");
      if (ageFromEl.value === ageFromEl.min) {
        ageFromEl.setAttribute("disabled", true);
      }
      const ageTillEl = $el.querySelector("#id_age_till");
      if (ageTillEl.value === ageTillEl.max) {
        ageTillEl.setAttribute("disabled", true);
      }
    },
    resetFilters(event) {
      const filterInputEls = $el.querySelectorAll(
        "#filters input, #filters select"
      );
      for (var i = 0; i < filterInputEls.length; i++) {
        const filterInputEl = filterInputEls[i];
        if (filterInputEl.id === event.target.id) {
          continue; // Don't reset the reset button value
        }
        if (filterInputEl.id === "id_language") {
          filterInputEl.value = filterInputEl.options[0].value;
        } else if (filterInputEl.id === "id_age_from") {
          filterInputEl.value = filterInputEl.min;
          filterInputEl.dispatchEvent(new Event("input"));
        } else if (filterInputEl.id === "id_age_till") {
          filterInputEl.value = filterInputEl.max;
          filterInputEl.dispatchEvent(new Event("input"));
        } else {
          filterInputEl.value = "";
        }
      }
    },
  };
}

function QueryInput() {
  return {
    clearQuery() {
      const queryInput = document.getElementById("id_query");
      // @ts-ignore
      queryInput.value = "";
      queryInput.focus();
    },
  };
}

/**
 * Inspired by the following resources:
 * https://tailwindcomponents.com/component/multi-range-slider
 * https://codepen.io/glitchworker/pen/XVdKqj
 * https://medium.com/@predragdavidovic10/native-dual-range-slider-html-css-javascript-91e778134816
 * https://codepen.io/alexerlandsson/pen/YzLeBVX
 * https://www.smashingmagazine.com/2021/12/create-custom-range-input-consistent-browsers/
 * https://developer.mozilla.org/en-US/docs/Web/HTML/Element/input/range
 */
function RangeSlider($refs) {
  return {
    init() {
      this.from = parseInt($refs.fromInput.value);
      this.till = parseInt($refs.tillInput.value);

      const fromStep = $refs.fromInput.step;
      const tillStep = $refs.tillInput.step;
      if (fromStep !== tillStep) {
        throw new Error("from and till steps should be equal");
      }
      this.step = parseInt(fromStep);
    },
    fromChange() {
      if (this.from >= this.till) {
        this.from = this.till - this.step;
      }
    },
    tillChange() {
      if (this.till <= this.from) {
        this.till = this.from + this.step;
      }
    },
  };
}
