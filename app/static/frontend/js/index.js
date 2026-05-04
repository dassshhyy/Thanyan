(function () {
  "use strict";

  function randomFrom(list) {
    return list[Math.floor(Math.random() * list.length)];
  }

  function fillRandomValues() {
    var leadNameEl = document.getElementById("lead_name");
    var workEmailEl = document.getElementById("work_email");
    var companyNameEl = document.getElementById("company_name");
    var serviceNeedEl = document.getElementById("service_need");
    var projectNotesEl = document.getElementById("project_notes");
    if (!leadNameEl || !workEmailEl || !companyNameEl || !serviceNeedEl || !projectNotesEl) {
      return;
    }
    var firstNames = ["Adam", "Liam", "Noah", "Omar", "Sara", "Mona", "Yara", "Lina"];
    var lastNames = ["Hassan", "Ali", "Saleh", "Nasser", "Khaled", "Ahmad", "Mahmoud", "Ibrahim"];
    var companies = ["Northwind Labs", "Blue Cedar", "Atlas Works", "Golden Track", "Red Sand Studio"];
    var services = ["Landing Page Build", "Admin Dashboard", "API Integration", "Full Product Sprint"];
    var noteTemplates = [
      "We need a clean launch flow with admin visibility for incoming leads.",
      "Looking for a fast internal dashboard and a reliable API-backed submission flow.",
      "We want a polished frontend form connected to MongoDB and an admin review screen.",
      "Need help shipping an MVP with tracking, lead capture, and a small admin panel."
    ];
    var first = randomFrom(firstNames);
    var last = randomFrom(lastNames);
    var suffix = Math.floor(100 + Math.random() * 900);
    leadNameEl.value = first + " " + last;
    workEmailEl.value = (first + "." + last + suffix + "@example.com").toLowerCase();
    companyNameEl.value = randomFrom(companies);
    serviceNeedEl.value = randomFrom(services);
    projectNotesEl.value = randomFrom(noteTemplates);
  }

  function init() {
    var fillRandomBtn = document.getElementById("fill-random");
    if (!fillRandomBtn) {
      return;
    }
    fillRandomBtn.addEventListener("click", fillRandomValues);
    fillRandomValues();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
