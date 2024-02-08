function SearchForm() {
  return {
    clear() {
      const queryInput = document.getElementById("query");
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
    algorithmChanged(event) {
      const selectedAlgorithm = event.target.value;
      const params = new URLSearchParams(window.location.search);
      params.set("algorithm", selectedAlgorithm);
      params.delete("page");
      params.delete("per_page");
      const newUrl = `${window.location.pathname}?${params.toString()}`;
      history.pushState(null, "", newUrl);
    },
  };
}
