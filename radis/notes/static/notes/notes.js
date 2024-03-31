function NoteList() {
  return {
    init() {
      document.body.addEventListener("noteDeleted", () => {
        location.reload();
      });
    },
  };
}
