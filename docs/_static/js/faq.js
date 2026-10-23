// Open a linked FAQ answer while leaving the question list collapsed by default.
(() => {
  const openLinkedAnswer = () => {
    const answer = document.getElementById(window.location.hash.slice(1));
    if (answer instanceof HTMLDetailsElement && answer.classList.contains("ha-faq")) {
      answer.open = true;
      answer.scrollIntoView({ block: "start" });
    }
  };
  window.addEventListener("hashchange", openLinkedAnswer);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", openLinkedAnswer);
  } else {
    openLinkedAnswer();
  }
})();
