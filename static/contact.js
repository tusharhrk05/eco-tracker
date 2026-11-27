document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".poll-btn").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const response = await fetch("/api/poll", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ choice: button.dataset.value }),
        });
        if (!response.ok) throw new Error("The vote could not be saved.");
        window.location.assign("/pollrs");
      } catch (error) {
        button.disabled = false;
        window.alert(error.message || "Could not save your vote. Please try again.");
      }
    });
  });
});
