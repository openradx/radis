function SearchForm() {
  return {
    clear() {
      const queryInput = document.getElementById("id_query");
      // @ts-ignore
      queryInput.value = "";
      queryInput.focus();

      const params = new URLSearchParams(window.location.search);
      params.delete("query");
      params.delete("page");
      params.delete("per_page");
      const newUrl = `${window.location.pathname}?${params.toString()}`;
      history.pushState(null, "", newUrl);
    },
    providerChanged(event) {
      const selectedProvider = event.target.value;
      const params = new URLSearchParams(window.location.search);
      params.set("provider", selectedProvider);
      params.delete("page");
      params.delete("per_page");
      const newUrl = `${window.location.pathname}?${params.toString()}`;
      history.pushState(null, "", newUrl);
    },
  };
}

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
