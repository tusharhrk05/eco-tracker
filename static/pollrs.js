document.addEventListener("DOMContentLoaded", async () => {
  try {
    const response = await fetch("/api/poll");
    if (!response.ok) throw new Error("Unable to load poll results.");
    const pollData = await response.json();
    const totalVotes = Object.values(pollData).reduce((sum, votes) => sum + votes, 0);

    document.querySelectorAll("[data-key]").forEach((bar) => {
      const count = pollData[bar.dataset.key] || 0;
      bar.style.width = `${totalVotes ? Math.round((count / totalVotes) * 100) : 0}%`;
    });
    document.querySelectorAll("[data-percent]").forEach((label) => {
      const count = pollData[label.dataset.percent] || 0;
      label.textContent = `${totalVotes ? Math.round((count / totalVotes) * 100) : 0}%`;
    });
  } catch (error) {
    const note = document.createElement("p");
    note.className = "subtitle";
    note.textContent = "Poll results are temporarily unavailable.";
    document.querySelector(".layout")?.append(note);
  }
});
