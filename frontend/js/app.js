/**
 * Surplus Router — Client Application Logic (Vanilla JavaScript)
 * Implements Zero-IDOR Capability Tokens, Strict XSS Escaping,
 * Role-Based View Switching, and Real-Time Coordination Feed.
 */

(function () {
  "use strict";

  // State Store
  const state = {
    activeTab: "donor",
    coordinatorToken: sessionStorage.getItem("coord_token") || null,
  };

  // Safe HTML Escaping utility to eliminate XSS injections
  function escapeHtml(unsafe) {
    if (typeof unsafe !== "string") {
      return String(unsafe ?? "");
    }
    return unsafe
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // DOM Elements
  const tabs = document.querySelectorAll(".nav-btn");
  const panels = document.querySelectorAll(".tab-panel");

  // Tab Navigation Switching
  function switchTab(targetTab) {
    state.activeTab = targetTab;
    tabs.forEach((btn) => {
      const isMatch = btn.dataset.tab === targetTab;
      btn.classList.toggle("active", isMatch);
      btn.setAttribute("aria-selected", isMatch ? "true" : "false");
    });

    panels.forEach((panel) => {
      const isMatch = panel.id === `tab-panel-${targetTab}`;
      panel.classList.toggle("active", isMatch);
    });

    if (targetTab === "impact") {
      fetchImpactMetrics();
    } else if (targetTab === "coordinator" && state.coordinatorToken) {
      loadCoordinatorFeeds();
    }
  }

  tabs.forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  // Set default datetime-local to 2 hours from now
  const readyByInput = document.getElementById("donor-input-ready-by");
  if (readyByInput) {
    const d = new Date(Date.now() + 2 * 3600 * 1000);
    const tzOffset = d.getTimezoneOffset() * 60000;
    const localISOTime = new Date(d.getTime() - tzOffset).toISOString().slice(0, 16);
    readyByInput.value = localISOTime;
  }

  // Pre-populate realistic demonstration coordinates matching active regional shelters
  const latInput = document.getElementById("donor-input-lat");
  const lonInput = document.getElementById("donor-input-lon");
  const addrInput = document.getElementById("donor-input-address");
  const nameInput = document.getElementById("donor-input-name");
  const idInput = document.getElementById("donor-input-id");
  const qtyInput = document.getElementById("donor-input-quantity");
  const phoneInput = document.getElementById("donor-input-phone");
  if (latInput && !latInput.value) latInput.value = "37.7760";
  if (lonInput && !lonInput.value) lonInput.value = "-122.4190";
  if (addrInput && !addrInput.value) addrInput.value = "500 Market St, Metro Core";
  if (nameInput && !nameInput.value) nameInput.value = "Golden Gate Artisan Bakery";
  if (idInput && !idInput.value) idInput.value = "donor-bakery-01";
  if (qtyInput && !qtyInput.value) qtyInput.value = "15.0";
  if (phoneInput && !phoneInput.value) phoneInput.value = "+14155550199";

  // Pre-populate Recipient, Volunteer, and Coordinator demo inputs
  const recIdInp = document.getElementById("recipient-input-id");
  const recTokInp = document.getElementById("recipient-input-token");
  const recCapInp = document.getElementById("recipient-input-capacity");
  if (recIdInp && !recIdInp.value) recIdInp.value = "rec-soup-kitchen-01";
  if (recTokInp && !recTokInp.value) recTokInp.value = "rec-secret-rec-soup-kitchen-01";
  if (recCapInp && !recCapInp.value) recCapInp.value = "100";

  const volIdInp = document.getElementById("volunteer-input-id");
  const volTokInp = document.getElementById("volunteer-input-token");
  if (volIdInp && !volIdInp.value) volIdInp.value = "vol-car-01";
  if (volTokInp && !volTokInp.value) volTokInp.value = "vol-secret-vol-car-01";

  const coordKeyInp = document.getElementById("coord-input-api-key");
  if (coordKeyInp && !coordKeyInp.value) coordKeyInp.value = "dev-insecure-coordinator-key-for-local-testing-only-32chars";

  // ---------------------------------------------------------------------------
  // 1. Donor Portal: Report Donation & Zero-IDOR Tracking
  // ---------------------------------------------------------------------------
  const donorForm = document.getElementById("donor-report-form");
  const donorStatus = document.getElementById("donor-form-status");

  if (donorForm) {
    donorForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      donorStatus.className = "alert-message hidden";

      const readyByStr = readyByInput.value;
      if (!readyByStr) {
        showStatus(donorStatus, "error", "Please specify a future ready-by time.");
        return;
      }
      const readyByIso = new Date(readyByStr).toISOString();

      let phone = document.getElementById("donor-input-phone").value.trim();
      if (phone && !phone.startsWith("+") && /^\d{10}$/.test(phone)) {
        phone = "+91" + phone;
      }

      const payload = {
        donor_name: document.getElementById("donor-input-name").value.trim(),
        donor_id: document.getElementById("donor-input-id").value.trim(),
        donor_phone: phone,
        food_category: document.getElementById("donor-select-category").value,
        quantity_kg: parseFloat(document.getElementById("donor-input-quantity").value),
        perishability_hours: parseFloat(document.getElementById("donor-input-perishability").value),
        ready_by: readyByIso,
        donor_address: document.getElementById("donor-input-address").value.trim(),
        donor_coordinates: {
          latitude: parseFloat(document.getElementById("donor-input-lat").value),
          longitude: parseFloat(document.getElementById("donor-input-lon").value),
        },
        service_region: "metro-core",
      };

      try {
        const res = await fetch("/api/donations", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });

        const data = await res.json();
        if (!res.ok) {
          let msg = data.error || "Failed reporting donation";
          if (data.details && Array.isArray(data.details)) {
            const detailStr = data.details.map(d => `${d.loc ? d.loc.slice(-1)[0] : 'field'}: ${d.msg}`).join('; ');
            msg += `: ${detailStr}`;
          }
          throw new Error(msg);
        }

        showStatus(donorStatus, "success", `Donation ${data.donation_id} registered and coordinated!`);
        // Populate tracking card and save token in session
        sessionStorage.setItem(`token_${data.donation_id}`, data.tracking_token);
        renderTrackingResult(data.donation_id, data.tracking_token, data.status, payload.food_category, payload.quantity_kg, payload.ready_by);
      } catch (err) {
        showStatus(donorStatus, "error", err.message);
      }
    });
  }

  // Donor Tracking Lookup
  const lookupForm = document.getElementById("donor-lookup-form");
  if (lookupForm) {
    lookupForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const donationId = document.getElementById("track-input-id").value.trim();
      const token = document.getElementById("track-input-token").value.trim();

      if (!donationId || !token) {
        alert("Both Donation ID and Tracking Token are required.");
        return;
      }

      try {
        const res = await fetch(`/api/donations/${encodeURIComponent(donationId)}`, {
          method: "GET",
          headers: { "X-Tracking-Token": token },
        });

        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || "Verification failed");
        }

        renderTrackingResult(
          data.donation_id,
          token,
          data.status,
          data.food_category,
          data.quantity_kg,
          data.ready_by,
          data.matched_recipient_name,
          data.assigned_volunteer_name,
          data.escalation_reason
        );
      } catch (err) {
        alert(`Access Denied: ${err.message}`);
      }
    });
  }

  function renderTrackingResult(id, token, status, category, qty, readyBy, recipient, volunteer, escalation) {
    const box = document.getElementById("donor-tracking-result");
    const pill = document.getElementById("track-status-pill");
    box.classList.remove("hidden");

    pill.textContent = status.toUpperCase();
    pill.className = `status-pill status-${status.toLowerCase()}`;

    document.getElementById("track-res-id").textContent = id;
    document.getElementById("track-res-cat-qty").textContent = `${qty} kg • ${category.replace("_", " ")}`;
    document.getElementById("track-res-recipient").textContent = recipient || "Matching in progress...";
    document.getElementById("track-res-volunteer").textContent = volunteer || "Awaiting transit assignment";
    document.getElementById("track-res-ready").textContent = new Date(readyBy).toLocaleString();

    const escRow = document.getElementById("track-escalation-row");
    if (escalation) {
      escRow.classList.remove("hidden");
      document.getElementById("track-res-escalation").textContent = escalation;
    } else {
      escRow.classList.add("hidden");
    }

    if (token) {
      const secretBox = document.getElementById("track-secret-token-display");
      secretBox.classList.remove("hidden");
      const tokenVal = document.getElementById("track-secret-token-val");
      tokenVal.dataset.fullToken = token;
      tokenVal.dataset.masked = "true";
      tokenVal.textContent = "●".repeat(38);
      const toggleBtn = document.getElementById("track-secret-toggle-btn");
      if (toggleBtn) {
        toggleBtn.textContent = "👁️ Show";
      }
    }
  }

  // Token masking Show/Hide & Copy Handlers
  const toggleSecretBtn = document.getElementById("track-secret-toggle-btn");
  const copySecretBtn = document.getElementById("track-secret-copy-btn");
  const tokenValElem = document.getElementById("track-secret-token-val");

  if (toggleSecretBtn && tokenValElem) {
    toggleSecretBtn.addEventListener("click", () => {
      const isMasked = tokenValElem.dataset.masked !== "false";
      if (isMasked) {
        tokenValElem.textContent = tokenValElem.dataset.fullToken || "";
        tokenValElem.dataset.masked = "false";
        toggleSecretBtn.textContent = "🔒 Hide";
      } else {
        tokenValElem.textContent = "●".repeat(38);
        tokenValElem.dataset.masked = "true";
        toggleSecretBtn.textContent = "👁️ Show";
      }
    });
  }

  if (copySecretBtn && tokenValElem) {
    copySecretBtn.addEventListener("click", async () => {
      const fullVal = tokenValElem.dataset.fullToken || "";
      if (fullVal) {
        try {
          await navigator.clipboard.writeText(fullVal);
          copySecretBtn.textContent = "✓ Copied!";
          setTimeout(() => {
            copySecretBtn.textContent = "📋 Copy";
          }, 2000);
        } catch (_) {
          copySecretBtn.textContent = "✓ Copied!";
        }
      }
    });
  }

  // ---------------------------------------------------------------------------
  // 2. Recipient Partner Check-in
  // ---------------------------------------------------------------------------
  const recForm = document.getElementById("recipient-checkin-form");
  const recStatus = document.getElementById("recipient-status-msg");

  if (recForm) {
    recForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      recStatus.className = "alert-message hidden";

      const recId = document.getElementById("recipient-input-id").value.trim();
      const token = document.getElementById("recipient-input-token").value.trim();
      const capacity = parseFloat(document.getElementById("recipient-input-capacity").value);
      const statusVal = document.querySelector('input[name="rec-status"]:checked').value;

      const prefs = [];
      if (document.getElementById("rec-pref-prepared").checked) prefs.push("prepared_meals");
      if (document.getElementById("rec-pref-produce").checked) prefs.push("produce");
      if (document.getElementById("rec-pref-bakery").checked) prefs.push("bakery");
      if (document.getElementById("rec-pref-halal").checked) prefs.push("halal");

      try {
        const res = await fetch(`/api/recipients/${encodeURIComponent(recId)}/capacity`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Recipient-Token": token,
          },
          body: JSON.stringify({
            capacity_kg_remaining: capacity,
            dietary_requirements: prefs,
            status: statusVal,
          }),
        });

        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Update failed");

        showStatus(recStatus, "success", `Partner capacity updated to ${capacity} kg (${statusVal})`);
      } catch (err) {
        showStatus(recStatus, "error", err.message);
      }
    });
  }

  // ---------------------------------------------------------------------------
  // 3. Volunteer Portal Check-in & Assignments
  // ---------------------------------------------------------------------------
  const volForm = document.getElementById("volunteer-checkin-form");
  const volStatus = document.getElementById("volunteer-status-msg");
  const volRefreshBtn = document.getElementById("volunteer-btn-refresh-assignments");

  if (volForm) {
    volForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      volStatus.className = "alert-message hidden";

      const volId = document.getElementById("volunteer-input-id").value.trim();
      const token = document.getElementById("volunteer-input-token").value.trim();
      const statusVal = document.querySelector('input[name="vol-status"]:checked').value;
      const vehicle = document.getElementById("volunteer-input-vehicle").value;
      const maxKg = parseFloat(document.getElementById("volunteer-input-max-kg").value) || 50.0;

      try {
        const res = await fetch(`/api/volunteers/${encodeURIComponent(volId)}/availability`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Volunteer-Token": token,
          },
          body: JSON.stringify({
            status: statusVal,
            vehicle_type: vehicle,
            max_capacity_kg: maxKg,
          }),
        });

        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Failed updating availability");

        showStatus(volStatus, "success", `Volunteer availability updated to ${statusVal}`);
        fetchVolunteerAssignments(volId, token);
      } catch (err) {
        showStatus(volStatus, "error", err.message);
      }
    });
  }

  if (volRefreshBtn) {
    volRefreshBtn.addEventListener("click", () => {
      const volId = document.getElementById("volunteer-input-id").value.trim();
      const token = document.getElementById("volunteer-input-token").value.trim();
      if (!volId || !token) {
        alert("Please enter your Volunteer ID and Access Token above first.");
        return;
      }
      fetchVolunteerAssignments(volId, token);
    });
  }

  async function fetchVolunteerAssignments(volId, token) {
    const container = document.getElementById("volunteer-assignments-container");
    container.innerHTML = '<p class="empty-state">Loading assigned deliveries...</p>';

    try {
      const res = await fetch(`/api/volunteers/${encodeURIComponent(volId)}/assignments`, {
        headers: { "X-Volunteer-Token": token },
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed fetching assignments");

      const items = data.assignments || [];
      if (items.length === 0) {
        container.innerHTML = '<p class="empty-state">No pending deliveries currently assigned to you.</p>';
        return;
      }

      container.innerHTML = items
        .map(
          (a) => `
        <div class="queue-card">
          <div class="queue-header">
            <strong>${escapeHtml(a.food_category.replace("_", " ").toUpperCase())} • ${escapeHtml(a.quantity_kg)} kg</strong>
            <span class="status-pill status-assigned">ASSIGNED</span>
          </div>
          <p class="queue-summary">📍 <strong>Pickup:</strong> ${escapeHtml(a.pickup_address)}</p>
          <p class="queue-summary">🏢 <strong>Deliver to:</strong> ${escapeHtml(a.delivery_organization)}</p>
          <p class="queue-summary">⏰ <strong>Ready:</strong> ${new Date(a.ready_by).toLocaleTimeString()}</p>
        </div>`
        )
        .join("");
    } catch (err) {
      container.innerHTML = `<p class="alert-text">Error: ${escapeHtml(err.message)}</p>`;
    }
  }

  // ---------------------------------------------------------------------------
  // 4. Coordinator Command Center (Authenticated)
  // ---------------------------------------------------------------------------
  const loginForm = document.getElementById("coordinator-login-form");
  const loginError = document.getElementById("coord-login-error");
  const authGate = document.getElementById("coord-auth-gate");
  const dashView = document.getElementById("coord-dashboard-view");
  const logoutBtn = document.getElementById("coord-btn-logout");
  const coordRefreshBtn = document.getElementById("coord-btn-refresh");

  if (loginForm) {
    loginForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      loginError.classList.add("hidden");

      const apiKey = document.getElementById("coord-input-api-key").value.trim();
      try {
        const res = await fetch("/api/coordinator/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ api_key: apiKey }),
        });

        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Authentication failed");

        state.coordinatorToken = data.token;
        sessionStorage.setItem("coord_token", data.token);
        renderCoordinatorDashboard();
      } catch (err) {
        loginError.textContent = err.message;
        loginError.classList.remove("hidden");
      }
    });
  }

  if (logoutBtn) {
    logoutBtn.addEventListener("click", () => {
      state.coordinatorToken = null;
      sessionStorage.removeItem("coord_token");
      authGate.classList.remove("hidden");
      dashView.classList.add("hidden");
    });
  }

  if (coordRefreshBtn) {
    coordRefreshBtn.addEventListener("click", () => loadCoordinatorFeeds());
  }

  function renderCoordinatorDashboard() {
    if (state.coordinatorToken) {
      authGate.classList.add("hidden");
      dashView.classList.remove("hidden");
      loadCoordinatorFeeds();
    } else {
      authGate.classList.remove("hidden");
      dashView.classList.add("hidden");
    }
  }

  async function loadCoordinatorFeeds() {
    if (!state.coordinatorToken) return;

    document.getElementById("coord-last-refresh").textContent = `Updated: ${new Date().toLocaleTimeString()}`;

    // 1. Fetch Escalations
    try {
      const escRes = await fetch("/api/coordinator/escalations", {
        headers: { Authorization: `Bearer ${state.coordinatorToken}` },
      });
      if (escRes.status === 401) {
        state.coordinatorToken = null;
        sessionStorage.removeItem("coord_token");
        renderCoordinatorDashboard();
        return;
      }
      const escData = await escRes.json();
      const escalations = escData.escalations || [];
      document.getElementById("coord-escalation-count").textContent = escalations.length;

      const escContainer = document.getElementById("coord-escalations-list");
      if (escalations.length === 0) {
        escContainer.innerHTML = '<p class="empty-state">No pending escalations. Autonomous coordination healthy.</p>';
      } else {
        escContainer.innerHTML = escalations
          .map(
            (e) => `
          <div class="queue-card border-alert">
            <div class="queue-header">
              <strong>${escapeHtml(e.donation_id)}</strong>
              <span class="status-pill status-escalated">${escapeHtml(e.escalation_reason || "ESCALATED")}</span>
            </div>
            <p class="queue-summary">${escapeHtml(e.donor_name)} • ${escapeHtml(e.quantity_kg)} kg • ${escapeHtml(e.food_category)}</p>
            <div class="queue-actions">
              <button class="btn btn-primary btn-sm" onclick="window.resolveTicket('${escapeHtml(e.donation_id)}', 'dismiss')">Dismiss Ticket</button>
            </div>
          </div>`
          )
          .join("");
      }
    } catch (err) {
      console.error("Failed loading escalations:", err);
    }

    // 2. Fetch Active Donations Pipeline
    try {
      const donRes = await fetch("/api/coordinator/donations", {
        headers: { Authorization: `Bearer ${state.coordinatorToken}` },
      });
      const donData = await donRes.json();
      const donations = donData.donations || [];
      document.getElementById("coord-donation-count").textContent = donations.length;

      const donContainer = document.getElementById("coord-donations-feed");
      if (donations.length === 0) {
        donContainer.innerHTML = '<p class="empty-state">No active donations in transit pipeline.</p>';
      } else {
        donContainer.innerHTML = donations
          .map(
            (d) => `
          <div class="queue-card">
            <div class="queue-header">
              <strong>${escapeHtml(d.donation_id)} • ${escapeHtml(d.donor_name)}</strong>
              <span class="status-pill status-${escapeHtml(d.status.toLowerCase())}">${escapeHtml(d.status)}</span>
            </div>
            <p class="queue-summary">${escapeHtml(d.quantity_kg)} kg • ${escapeHtml(d.food_category.replace("_", " "))} • Ready: ${new Date(d.ready_by).toLocaleTimeString()}</p>
          </div>`
          )
          .join("");
      }
    } catch (err) {
      console.error("Failed loading donations:", err);
    }
  }

  // Global resolve function for coordinator actions
  window.resolveTicket = async function (donationId, action) {
    if (!state.coordinatorToken) return;
    const notes = prompt(`Enter resolution notes for donation ${donationId}:`, "Manual coordinator verification complete.");
    if (!notes) return;

    try {
      const res = await fetch(`/api/coordinator/escalations/${encodeURIComponent(donationId)}/resolve`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${state.coordinatorToken}`,
        },
        body: JSON.stringify({
          resolution_action: action,
          notes: notes,
        }),
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Resolution failed");

      alert(`Resolution Recorded: ${data.message}`);
      loadCoordinatorFeeds();
    } catch (err) {
      alert(`Error resolving escalation: ${err.message}`);
    }
  };

  // ---------------------------------------------------------------------------
  // 5. Impact Metrics Summary
  // ---------------------------------------------------------------------------
  async function fetchImpactMetrics() {
    try {
      const res = await fetch("/api/summary");
      if (!res.ok) return;
      const data = await res.json();

      document.getElementById("metric-kg-routed").textContent = `${data.total_kg_routed.toLocaleString()} kg`;
      document.getElementById("metric-meals-routed").textContent = Math.round(data.meals_equivalent).toLocaleString();
      document.getElementById("metric-orgs-served").textContent = data.organizations_served;
      document.getElementById("metric-active-volunteers").textContent = data.active_volunteers;
    } catch (err) {
      console.error("Failed fetching impact summary:", err);
    }
  }

  // Helper function to render status messages
  function showStatus(elem, type, message) {
    elem.textContent = message;
    elem.className = `alert-message alert-${type}`;
  }

  // Initialize view
  if (state.coordinatorToken) {
    renderCoordinatorDashboard();
  }
  fetchImpactMetrics();
})();
