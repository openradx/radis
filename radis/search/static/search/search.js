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
