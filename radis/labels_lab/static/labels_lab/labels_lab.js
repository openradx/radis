"use strict";

// htmx swaps nothing in for an error response or for a request that never got through,
// so a failed run would look like one that was never started. Say what happened instead.
document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("lab-form");
  const notice = document.getElementById("lab-request-error");
  if (!form || !notice) return;

  function show(message) {
    notice.textContent = message;
    notice.classList.remove("d-none");
  }

  form.addEventListener("htmx:beforeRequest", () => {
    notice.classList.add("d-none");
  });
  form.addEventListener("htmx:responseError", (event) => {
    const { xhr, pathInfo } = event.detail;
    show(
      `${pathInfo.requestPath} answered ${xhr.status} ${xhr.statusText}. ` +
        "Nothing on this page was updated; the web container's log has the details."
    );
  });
  form.addEventListener("htmx:sendError", (event) => {
    show(
      `${event.detail.pathInfo.requestPath} could not be reached. Nothing on this page was updated.`
    );
  });
});
