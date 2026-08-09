(() => {
  const sectionButtons = [...document.querySelectorAll("[data-section-tab]")];
  const sectionPanels = [...document.querySelectorAll("[data-section-panel]")];

  function activateSection(target) {
    sectionButtons.forEach((button) => {
      const active = button.dataset.sectionTab === target;
      button.setAttribute("aria-selected", String(active));
    });
    sectionPanels.forEach((panel) => {
      panel.hidden = panel.dataset.sectionPanel !== target;
    });
  }

  sectionButtons.forEach((button) => button.addEventListener("click", () => activateSection(button.dataset.sectionTab)));

  document.querySelectorAll("[data-direction-group]").forEach((group) => {
    const buttons = [...group.querySelectorAll("[data-direction-tab]")];
    const section = group.closest(".proposal-section");
    const panels = [...(section?.querySelectorAll("[data-direction-panel]") || [])];
    buttons.forEach((button) => button.addEventListener("click", () => {
      const target = button.dataset.directionTab;
      buttons.forEach((item) => item.setAttribute("aria-selected", String(item === button)));
      panels.forEach((panel) => { panel.hidden = panel.dataset.directionPanel !== target; });
    }));
  });
})();
