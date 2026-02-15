const state = {
  sessionToken: localStorage.getItem("lobster_session_token") || "",
  userEmail: localStorage.getItem("lobster_user_email") || "",
  profileId: localStorage.getItem("lobster_profile_id") || "",
  profiles: [],
  profileIndex: 0,
  selectedNegotiation: null,
  selectedNegotiationStatus: "",
  agentMessages: [],
  carouselIndex: 0,
  carouselTimer: null,
  authMode: "login",
};

const el = (id) => document.getElementById(id);
const page = document.body.dataset.page || "";

const heroPeople = [
  {
    label: "Sofia",
    image: "/static/images/woman-1.png",
  },
  {
    label: "Mina",
    image: "/static/images/woman-2.png",
  },
  {
    label: "Claire",
    image: "/static/images/woman-3.png",
  },
  {
    label: "Adrian",
    image: "/static/images/man-1.jpg",
  },
  {
    label: "Kai",
    image: "/static/images/man-2.jpg",
  },
  {
    label: "Leo",
    image: "/static/images/man-3.jpg",
  },
];

const deckPhotos = [
  "/static/images/woman-1.png",
  "/static/images/woman-2.png",
  "/static/images/woman-3.png",
  "/static/images/man-1.jpg",
  "/static/images/man-2.jpg",
  "/static/images/man-3.jpg",
];

const FLOW_ORDER = ["discover", "inbox", "chat", "decision"];

function log(message, data = null) {
  const ts = new Date().toLocaleTimeString();
  const payload = data ? `\n${JSON.stringify(data, null, 2)}` : "";
  const line = `[${ts}] ${message}${payload}`;
  console.log(line);

  const notice = el("appNotice");
  if (notice) {
    const shouldSurface = !/(refreshed|dashboard ready)/i.test(message);
    if (shouldSurface) {
      const isError = /(failed|error|could not|unavailable|missing|invalid)/i.test(message);
      notice.classList.toggle("error", isError);
      notice.textContent = message;
    }
  }
}

function setAuthStatus(message) {
  const statusEl = el("authStatus");
  if (statusEl) statusEl.textContent = message;
}

function setProfileStatus(message) {
  const statusEl = el("profileStatus");
  if (statusEl) statusEl.textContent = message;
}

function setNextStep(message) {
  const hint = el("nextStepHint");
  if (hint) hint.textContent = message;
}

function scrollToModule(id) {
  const section = el(id);
  if (section) section.scrollIntoView({ behavior: "smooth", block: "start" });
}

function focusMessageComposer() {
  const input = el("messageInput");
  if (input) input.focus();
}

function otherPartyProfile(negotiation) {
  if (!negotiation) return null;
  if (negotiation.initiator_id === state.profileId) return negotiation.target_profile || null;
  return negotiation.initiator_profile || null;
}

function syncMobileActionState() {
  const sendBtn = el("mobileSendMessage");
  if (sendBtn) {
    sendBtn.disabled = !state.selectedNegotiation;
  }
}

function setFlowStep(step) {
  const activeIndex = FLOW_ORDER.indexOf(step);
  if (activeIndex < 0) return;
  const steps = document.querySelectorAll(".flow-step");
  steps.forEach((node) => {
    const nodeStep = node.dataset.step || "";
    const idx = FLOW_ORDER.indexOf(nodeStep);
    node.classList.toggle("active", idx === activeIndex);
    node.classList.toggle("done", idx >= 0 && idx < activeIndex);
  });
}

function setDecisionControls(status = "") {
  const decision = el("decision");
  const reason = el("decisionReason");
  const submit = el("submitDecision");
  const viewContactBtn = el("viewContact");
  const hint = el("decisionHint");
  if (!decision || !reason || !submit || !viewContactBtn || !hint) return;

  if (!state.selectedNegotiation) {
    decision.disabled = true;
    reason.disabled = true;
    submit.disabled = true;
    viewContactBtn.disabled = true;
    hint.textContent = "Select a conversation to unlock decisions.";
    syncMobileActionState();
    return;
  }

  if (status === "match") {
    decision.disabled = true;
    reason.disabled = true;
    submit.disabled = true;
    viewContactBtn.disabled = false;
    hint.textContent = "Match confirmed. You can now view contact details.";
    syncMobileActionState();
    return;
  }

  if (status === "no_match" || status === "rejected") {
    decision.disabled = true;
    reason.disabled = true;
    submit.disabled = true;
    viewContactBtn.disabled = true;
    hint.textContent = "This conversation is closed. No further decision is needed.";
    syncMobileActionState();
    return;
  }

  if (status === "accepted" || status === "talking") {
    decision.disabled = false;
    reason.disabled = false;
    submit.disabled = false;
    viewContactBtn.disabled = true;
    hint.textContent = "When ready, submit your decision. Contact unlocks after mutual match.";
    syncMobileActionState();
    return;
  }

  decision.disabled = true;
  reason.disabled = true;
  submit.disabled = true;
  viewContactBtn.disabled = true;
  hint.textContent = "Accept the request first, then continue in chat.";
  syncMobileActionState();
}

function saveAuthSession() {
  localStorage.setItem("lobster_session_token", state.sessionToken);
  localStorage.setItem("lobster_user_email", state.userEmail);
  localStorage.setItem("lobster_profile_id", state.profileId);
}

function clearAuthSession() {
  state.sessionToken = "";
  state.userEmail = "";
  state.profileId = "";
  localStorage.removeItem("lobster_session_token");
  localStorage.removeItem("lobster_user_email");
  localStorage.removeItem("lobster_profile_id");
}

async function api(path, { method = "GET", body = null, auth = true } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (auth) {
    if (!state.sessionToken) {
      throw new Error("Please log in first.");
    }
    headers.Authorization = `Bearer ${state.sessionToken}`;
  }

  const response = await fetch(path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : null,
  });

  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = { error: "Invalid response" };
  }

  if (!response.ok) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }

  return payload;
}

async function loadSession() {
  if (!state.sessionToken) return null;
  try {
    const out = await api("/api/auth/me");
    state.userEmail = out.user?.email || "";
    state.profileId = out.user?.profile_id || "";
    saveAuthSession();
    return out.user || null;
  } catch {
    clearAuthSession();
    return null;
  }
}

async function performLogin(email, password) {
  const out = await api("/api/auth/login", {
    method: "POST",
    body: { email, password },
    auth: false,
  });

  state.sessionToken = out.session_token;
  state.userEmail = out.user?.email || email;
  state.profileId = out.user?.profile_id || "";
  saveAuthSession();

  return out;
}

function setAuthMode(mode) {
  state.authMode = mode === "signup" ? "signup" : "login";
  const isSignUp = state.authMode === "signup";

  const title = el("authPanelTitle");
  const sub = el("authPanelSub");
  const submit = el("authSubmit");
  const tabLogin = el("authTabLogin");
  const tabSignup = el("authTabSignup");

  if (title) title.textContent = isSignUp ? "Sign Up" : "Login";
  if (sub) {
    sub.textContent = isSignUp
      ? "Create account with email and password. Then create your profile."
      : "Enter your email and password to continue.";
  }
  if (submit) submit.textContent = isSignUp ? "Create Account" : "Login";

  if (tabLogin) {
    tabLogin.classList.toggle("active", !isSignUp);
    tabLogin.classList.toggle("primary", !isSignUp);
    tabLogin.classList.toggle("ghost", isSignUp);
  }
  if (tabSignup) {
    tabSignup.classList.toggle("active", isSignUp);
    tabSignup.classList.toggle("primary", isSignUp);
    tabSignup.classList.toggle("ghost", !isSignUp);
  }
}

async function signUpFromAuth() {
  try {
    const email = el("authEmail").value.trim();
    const password = el("authPassword").value;

    await api("/api/auth/signup", {
      method: "POST",
      body: { email, password },
      auth: false,
    });

    setAuthStatus("Account created. Logging you in...");
    await performLogin(email, password);
    window.location.assign("/profile");
  } catch (error) {
    setAuthStatus(`Sign up failed: ${error.message}`);
  }
}

async function loginFromAuth() {
  try {
    const email = el("authEmail").value.trim();
    const password = el("authPassword").value;
    const out = await performLogin(email, password);
    setAuthStatus(`Logged in as ${state.userEmail}`);

    if (out.user?.has_profile) {
      window.location.assign("/app");
      return;
    }
    window.location.assign("/profile");
  } catch {
    setAuthStatus("Incorrect username or password");
  }
}

async function logOut() {
  try {
    if (state.sessionToken) {
      await api("/api/auth/logout", { method: "POST" });
    }
  } catch {
    // Ignore logout network/auth errors and clear local session anyway.
  } finally {
    clearAuthSession();
    window.location.assign("/auth?mode=login");
  }
}

function csv(value) {
  return value
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean);
}

function toAgeRange() {
  const min = Number(el("preferredMinAge").value);
  const max = Number(el("preferredMaxAge").value);
  if (!Number.isFinite(min) || !Number.isFinite(max)) return null;
  return [Math.min(min, max), Math.max(min, max)];
}

function optionalNumber(id) {
  const raw = el(id).value.trim();
  if (!raw) return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}

async function registerProfile() {
  try {
    const ageRange = toAgeRange();
    const contact = {
      email: el("contactEmail").value.trim(),
      telegram: el("contactTelegram").value.trim(),
      whatsapp: el("contactWhatsapp").value.trim(),
      instagram: el("contactInstagram").value.trim(),
      discord: el("contactDiscord").value.trim(),
    };

    Object.keys(contact).forEach((key) => {
      if (!contact[key]) delete contact[key];
    });

    const payload = {
      display_name: el("displayName").value.trim(),
      profile: {
        age: Number(el("age").value),
        gender: el("gender").value.trim(),
        location_city: el("city").value.trim(),
        location_country: el("country").value.trim(),
        appearance_summary: el("appearanceSummary").value.trim(),
        height_cm: optionalNumber("heightCm"),
        body_type: el("bodyType").value.trim(),
        personality_summary: el("personalitySummary").value.trim(),
        interests: csv(el("interests").value),
        values: csv(el("values").value),
        communication_style: el("communicationStyle").value.trim(),
        pref_age_min: ageRange ? ageRange[0] : null,
        pref_age_max: ageRange ? ageRange[1] : null,
        pref_gender: csv(el("preferredGenders").value),
        pref_location: el("preferredLocation").value.trim(),
        pref_summary: el("preferredSummary").value.trim(),
        dealbreakers: csv(el("dealbreakers").value),
        photos: csv(el("photoUrls").value),
      },
      contact,
    };

    const out = await api("/api/agents/register", {
      method: "POST",
      body: payload,
      auth: true,
    });

    state.profileId = out.profile_id || state.profileId;
    saveAuthSession();
    const connected = out.datingopenclaw?.connected ? " Dating API connected." : "";
    setProfileStatus(`Profile created.${connected} Redirecting to dashboard...`);
    window.location.assign("/app");
  } catch (error) {
    setProfileStatus(`Profile creation failed: ${error.message}`);
  }
}

function renderCarousel() {
  const root = el("carousel");
  if (!root) return;
  root.innerHTML = heroPeople
    .map((p, idx) => {
      const active = idx === state.carouselIndex ? "active" : "";
      return `<div class="slide ${active}" style="background-image:url('${p.image}')"><div class="slide-caption">${p.label}</div></div>`;
    })
    .join("");
}

function stepCarousel(dir) {
  state.carouselIndex = (state.carouselIndex + dir + heroPeople.length) % heroPeople.length;
  renderCarousel();
}

function startCarouselAutoplay() {
  if (state.carouselTimer) {
    clearInterval(state.carouselTimer);
  }
  state.carouselTimer = setInterval(() => stepCarousel(1), 3500);
}

function profileImage(profile) {
  let n = 0;
  for (const c of profile.id) n += c.charCodeAt(0);
  return deckPhotos[n % deckPhotos.length];
}

function currentProfile() {
  return state.profiles[state.profileIndex] || null;
}

function renderDeckSwitcher() {
  const picker = el("profilePicker");
  const prevBtn = el("prevProfile");
  const nextBtn = el("nextProfile");
  const counter = el("deckCounter");
  if (!picker || !prevBtn || !nextBtn || !counter) return;

  const total = state.profiles.length;
  const hasProfiles = total > 0;
  const clampedIndex = hasProfiles ? Math.min(Math.max(state.profileIndex, 0), total - 1) : 0;
  const profile = currentProfile();

  picker.innerHTML = "";
  if (!hasProfiles) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "Load profiles first";
    picker.appendChild(option);
    picker.disabled = true;
    prevBtn.disabled = true;
    nextBtn.disabled = true;
    counter.textContent = "No profiles loaded.";
    return;
  }

  for (let i = 0; i < total; i += 1) {
    const p = state.profiles[i];
    const option = document.createElement("option");
    option.value = String(i);
    const location = [p.city, p.country].filter(Boolean).join(", ");
    option.textContent = location ? `${p.display_name}, ${p.age} · ${location}` : `${p.display_name}, ${p.age}`;
    picker.appendChild(option);
  }

  picker.disabled = false;
  picker.value = String(clampedIndex);
  prevBtn.disabled = state.profileIndex <= 0;
  nextBtn.disabled = state.profileIndex >= total - 1;
  counter.textContent = profile
    ? `Viewing ${state.profileIndex + 1} of ${total}`
    : `You've reached the end of ${total} loaded profiles.`;
}

function renderDeck() {
  const root = el("deck");
  if (!root) return;
  renderDeckSwitcher();

  const profile = currentProfile();
  if (!profile) {
    root.innerHTML = '<div class="deck-card" style="background:#f3e6d6"><div class="deck-content"><h3>No more profiles</h3><p>Load profiles again to continue.</p></div></div>';
    return;
  }

  const tags = [...(profile.interests || []), ...(profile.values || [])].slice(0, 6);
  root.innerHTML = `
    <article class="deck-card" style="background-image:url('${profileImage(profile)}')">
      <div class="deck-content">
        <h3>${profile.display_name}, ${profile.age}</h3>
        <p>${profile.gender || ""} · ${profile.city || ""} ${profile.country || ""}</p>
        <p>${profile.personality || "No personality summary."}</p>
        <div class="pills">${tags.map((t) => `<span>${t}</span>`).join("")}</div>
      </div>
    </article>
  `;
}

async function loadProfiles() {
  try {
    const params = new URLSearchParams();
    const raw = {
      gender: el("filterGender").value.trim(),
      city: el("filterCity").value.trim(),
      country: el("filterCountry").value.trim(),
      source: "local",
      page: "1",
      limit: "50",
    };

    Object.entries(raw).forEach(([k, v]) => v && params.set(k, v));

    const out = await api(`/api/agents/profiles?${params.toString()}`);
    state.profiles = out.profiles || [];
    state.profileIndex = 0;
    renderDeck();
    log(`Loaded ${state.profiles.length} profiles.`);
    if (state.profiles.length > 0) {
      setNextStep("Review a profile and tap Like to send a request.");
      setFlowStep("discover");
    } else {
      setNextStep("No matches in this filter. Try another city or clear filters.");
    }
  } catch (error) {
    log(`Could not load profiles: ${error.message}`);
    setNextStep("Unable to load profiles right now. Try again.");
  }
}

function previousProfile() {
  if (state.profileIndex <= 0) {
    renderDeck();
    return;
  }
  state.profileIndex -= 1;
  renderDeck();
}

function nextProfile() {
  if (state.profileIndex >= state.profiles.length - 1) {
    renderDeck();
    return;
  }
  state.profileIndex += 1;
  renderDeck();
}

function jumpToProfile(event) {
  const value = Number.parseInt(event.target.value, 10);
  if (Number.isNaN(value) || value < 0 || value >= state.profiles.length) return;
  state.profileIndex = value;
  renderDeck();
}

function passProfile() {
  const p = currentProfile();
  if (!p) {
    log("No profile to pass.");
    return;
  }
  log(`Passed ${p.display_name}.`);
  state.profileIndex += 1;
  renderDeck();
}

async function likeProfile() {
  const p = currentProfile();
  if (!p) {
    log("No profile to like.");
    return;
  }
  if (p.source === "datingopenclaw") {
    log(
      `This is a DatingOpenClaw search result (${p.id}). Use Agent Chat to negotiate: /dating negotiate target_id=${p.id}`,
    );
    setNextStep("For this profile, use Agent Chat below to send the request.");
    return;
  }

  try {
    const intro = el("introMessage").value.trim() || "Hey, our humans might align. Open to chat?";
    const out = await api("/api/negotiations", {
      method: "POST",
      body: {
        target_id: p.id,
        intro_message: intro,
      },
    });
    log(`Liked ${p.display_name} and opened negotiation.`, out);
    state.profileIndex += 1;
    renderDeck();
    refreshInbox();
    refreshConversations();
    setNextStep("Request sent. Check Inbox for replies or continue discovering profiles.");
    setFlowStep("inbox");
  } catch (error) {
    log(`Like failed: ${error.message}`);
    setNextStep("Could not send request. Please try again.");
  }
}

function incomingItem(n) {
  const sender = n.initiator_profile || {};
  const senderName = sender.display_name || n.initiator_id.slice(0, 8);
  const senderAge = sender.age ? `, ${sender.age}` : "";
  const senderLocation = [sender.city, sender.country].filter(Boolean).join(", ");
  const senderMeta = [sender.gender, senderLocation].filter(Boolean).join(" · ");
  const senderPersonality = sender.personality || "";
  const introMessage = n.intro_message || "";

  const row = document.createElement("div");
  row.className = "item";
  const title = document.createElement("strong");
  title.textContent = `${senderName}${senderAge} · ${n.status}`;
  row.appendChild(title);

  const meta = document.createElement("p");
  meta.textContent = senderMeta || `${n.initiator_id.slice(0, 8)} → ${n.target_id.slice(0, 8)}`;
  row.appendChild(meta);

  if (senderPersonality) {
    const personality = document.createElement("p");
    personality.textContent = senderPersonality;
    row.appendChild(personality);
  }

  if (introMessage) {
    const intro = document.createElement("p");
    intro.textContent = `Intro: ${introMessage}`;
    row.appendChild(intro);
  }

  const actions = document.createElement("div");
  actions.className = "inline";
  const acceptBtn = document.createElement("button");
  acceptBtn.className = "btn secondary";
  acceptBtn.type = "button";
  acceptBtn.textContent = "Accept & Open Chat";
  const rejectBtn = document.createElement("button");
  rejectBtn.className = "btn danger";
  rejectBtn.type = "button";
  rejectBtn.textContent = "Reject";
  const openBtn = document.createElement("button");
  openBtn.className = "btn ghost";
  openBtn.type = "button";
  openBtn.textContent = "Open Details";
  actions.appendChild(acceptBtn);
  actions.appendChild(rejectBtn);
  actions.appendChild(openBtn);
  row.appendChild(actions);

  acceptBtn.onclick = () => acceptReject(n.id, true);
  rejectBtn.onclick = () => acceptReject(n.id, false);
  openBtn.onclick = () => selectNegotiation(n.id);
  return row;
}

function convoItem(n) {
  const other = otherPartyProfile(n);
  const otherName = other?.display_name || "Conversation";
  const otherAge = other?.age ? `, ${other.age}` : "";
  const metaLine = [other?.gender, other?.city, other?.country].filter(Boolean).join(" · ");

  const row = document.createElement("div");
  row.className = "item";
  const title = document.createElement("strong");
  title.textContent = `${otherName}${otherAge} · ${n.status.toUpperCase()}`;
  const detail = document.createElement("p");
  detail.textContent = metaLine || `Updated ${n.updated_at}`;
  const button = document.createElement("button");
  button.className = "btn ghost";
  button.type = "button";
  button.textContent = "Open Chat";
  button.onclick = () => selectNegotiation(n.id);

  row.appendChild(title);
  row.appendChild(detail);
  row.appendChild(button);
  return row;
}

async function refreshInbox() {
  try {
    const out = await api("/api/negotiations?type=incoming&status=requested");
    const root = el("incomingList");
    if (!root) return;

    root.innerHTML = "";
    if (!out.negotiations.length) {
      root.innerHTML = '<p class="meta">No incoming requests.</p>';
      setNextStep("No incoming requests right now. Continue discovering profiles.");
      return;
    }
    out.negotiations.forEach((n) => root.appendChild(incomingItem(n)));
    log(`Inbox refreshed (${out.negotiations.length}).`);
    setNextStep("You have incoming requests. Tap Accept & Open Chat to continue.");
    if (!state.selectedNegotiation) {
      setFlowStep("inbox");
    }
  } catch (error) {
    log(`Inbox refresh failed: ${error.message}`);
    setNextStep("Could not load inbox right now. Try refresh.");
  }
}

async function refreshConversations() {
  try {
    const out = await api("/api/negotiations?type=all&status=accepted,talking,match,no_match");
    const root = el("convoList");
    if (!root) return;

    root.innerHTML = "";
    if (!out.negotiations.length) {
      root.innerHTML = '<p class="meta">No active conversations.</p>';
      return;
    }
    out.negotiations.forEach((n) => root.appendChild(convoItem(n)));
    if (state.selectedNegotiation) {
      const selected = out.negotiations.find((n) => n.id === state.selectedNegotiation);
      if (selected?.status) {
        state.selectedNegotiationStatus = selected.status;
        setDecisionControls(state.selectedNegotiationStatus);
      }
    }
    log(`Conversations refreshed (${out.negotiations.length}).`);
  } catch (error) {
    log(`Conversation refresh failed: ${error.message}`);
  }
}

async function acceptReject(id, accept) {
  try {
    const action = accept ? "accept" : "reject";
    const out = await api(`/api/negotiations/${id}/${action}`, { method: "POST" });
    log(`${action} succeeded.`, out);
    await refreshInbox();
    await refreshConversations();
    if (accept) {
      state.selectedNegotiationStatus = "accepted";
      await selectNegotiation(id, { fromAccept: true });
      scrollToModule("chat");
      focusMessageComposer();
      return;
    }
    state.selectedNegotiation = null;
    state.selectedNegotiationStatus = "";
    setDecisionControls();
    setFlowStep("inbox");
    setNextStep("Request rejected. Review other incoming requests or discover new profiles.");
  } catch (error) {
    log(`${accept ? "Accept" : "Reject"} failed: ${error.message}`);
    setNextStep("Action failed. Please try again.");
  }
}

async function selectNegotiation(id, { fromAccept = false } = {}) {
  state.selectedNegotiation = id;
  state.selectedNegotiationStatus = "";
  setDecisionControls();
  const meta = el("chatMeta");
  let displayName = id;
  let hint = "Send a thoughtful message, then submit your decision when ready.";
  try {
    const details = await api(`/api/negotiations/${id}`);
    state.selectedNegotiationStatus = details.status || "";
    const other = otherPartyProfile(details);
    displayName = other?.display_name || id;
    if (meta) {
      if (details.status === "match") {
        meta.textContent = `Conversation with ${displayName}. You matched. Use View Contact below.`;
        hint = "This conversation is a match. Tap View Contact to reveal contact details.";
        setFlowStep("decision");
      } else if (details.status === "no_match") {
        meta.textContent = `Conversation with ${displayName}. Closed as no match.`;
        hint = "This conversation is closed as no match. Continue with other profiles.";
        setFlowStep("decision");
      } else {
        meta.textContent = `Conversation with ${displayName}. Send a thoughtful first message.`;
        setFlowStep("chat");
      }
    }
  } catch {
    if (meta) meta.textContent = `Selected conversation: ${id}`;
    setFlowStep("chat");
  }
  setDecisionControls(state.selectedNegotiationStatus);
  await loadMessages();
  if (fromAccept) {
    setNextStep(`Accepted ${displayName}. Send your first message now.`);
  } else {
    setNextStep(hint);
  }
}

async function loadMessages() {
  if (!state.selectedNegotiation) {
    log("Select a negotiation first.");
    setNextStep("Pick a conversation first, then send your message.");
    return;
  }

  try {
    const out = await api(`/api/negotiations/${state.selectedNegotiation}/messages`);
    const root = el("messageList");
    if (!root) return;

    root.innerHTML = "";
    if (!out.messages.length) {
      root.innerHTML = '<p class="meta">No messages yet.</p>';
      return;
    }

    out.messages.forEach((m) => {
      const row = document.createElement("div");
      row.className = `msg ${m.sender_id === state.profileId ? "mine" : ""}`;
      row.innerHTML = `<strong>${m.sender_id.slice(0, 8)}</strong><div>${m.content || m.body || ""}</div>`;
      root.appendChild(row);
    });
  } catch (error) {
    log(`Load messages failed: ${error.message}`);
    setNextStep("Could not load messages. Try again.");
  }
}

async function sendMessage() {
  if (!state.selectedNegotiation) {
    log("Select a negotiation first.");
    setNextStep("Pick a conversation first.");
    return;
  }

  const input = el("messageInput");
  const body = input.value.trim();
  if (!body) {
    log("Message is empty.");
    setNextStep("Write a short message before sending.");
    return;
  }

  try {
    const out = await api(`/api/negotiations/${state.selectedNegotiation}/messages`, {
      method: "POST",
      body: { message: body },
    });
    input.value = "";
    log("Message sent.", out);
    loadMessages();
    refreshConversations();
    setFlowStep("chat");
    setNextStep("Message sent. Continue the chat, then submit final decision when ready.");
  } catch (error) {
    log(`Send failed: ${error.message}`);
    setNextStep("Message failed to send. Try again.");
  }
}

async function submitDecision() {
  if (!state.selectedNegotiation) {
    log("Select a negotiation first.");
    setNextStep("Open a conversation first, then submit your decision.");
    return;
  }

  try {
    const out = await api(`/api/negotiations/${state.selectedNegotiation}/result`, {
      method: "POST",
      body: {
        decision: el("decision").value,
        reason: el("decisionReason").value.trim(),
      },
    });
    state.selectedNegotiationStatus = out.status || state.selectedNegotiationStatus;
    setDecisionControls(state.selectedNegotiationStatus);
    log("Decision submitted.", out);
    refreshConversations();
    setFlowStep("decision");
    if (out.status === "match") {
      setNextStep("It's a match. Tap View Contact to reveal contact details.");
    } else {
      setNextStep("Decision submitted. You can continue with other conversations.");
    }
  } catch (error) {
    log(`Decision failed: ${error.message}`);
    setNextStep("Could not submit decision. Try again.");
  }
}

async function viewContact() {
  if (!state.selectedNegotiation) {
    log("Select a negotiation first.");
    setNextStep("Open a matched conversation first.");
    return;
  }

  try {
    const out = await api(`/api/negotiations/${state.selectedNegotiation}/contact`);
    el("contactOut").textContent = JSON.stringify(out, null, 2);
    log("Contact loaded.", out);
    setFlowStep("decision");
    setNextStep("Contact unlocked. You can now reach out directly.");
  } catch (error) {
    log(`Contact unavailable: ${error.message}`);
    setNextStep("Contact is available only after a mutual match.");
  }
}

function renderAgentChat() {
  const root = el("agentThread");
  if (!root) return;
  root.innerHTML = "";
  if (!state.agentMessages.length) {
    root.innerHTML = '<p class="meta">Ask the agent to search, inspect inbox, or summarize matches.</p>';
    return;
  }
  state.agentMessages.forEach((msg) => {
    const row = document.createElement("div");
    row.className = `agent-msg ${msg.role === "user" ? "mine" : ""}`;
    const label = msg.role === "user" ? "You" : "Agent";
    row.innerHTML = `<strong>${label}</strong><div></div>`;
    row.querySelector("div").textContent = msg.content;
    root.appendChild(row);
  });
  root.scrollTop = root.scrollHeight;
}

async function loadAgentChatHistory() {
  try {
    const out = await api("/api/agent/chat/history");
    state.agentMessages = out.messages || [];
    renderAgentChat();
  } catch (error) {
    log(`Agent chat history failed: ${error.message}`);
  }
}

async function sendAgentMessage() {
  const input = el("agentInput");
  if (!input) return;
  const message = input.value.trim();
  if (!message) return;

  state.agentMessages.push({ role: "user", content: message });
  renderAgentChat();
  input.value = "";
  try {
    const out = await api("/api/agent/chat", {
      method: "POST",
      body: { message },
    });
    state.agentMessages.push({
      role: "assistant",
      content: out.reply || "No reply",
    });
    renderAgentChat();
  } catch (error) {
    state.agentMessages.push({
      role: "assistant",
      content: `Agent error: ${error.message}`,
    });
    renderAgentChat();
  }
}

async function clearAgentChat() {
  try {
    const out = await api("/api/agent/chat/clear", {
      method: "POST",
    });
    state.agentMessages = [];
    renderAgentChat();
    log(`Agent chat cleared (${out.deleted || 0} messages).`);
  } catch (error) {
    log(`Could not clear agent chat: ${error.message}`);
  }
}

function bindLandingEvents() {
  el("carouselPrev").onclick = () => {
    stepCarousel(-1);
    startCarouselAutoplay();
  };
  el("carouselNext").onclick = () => {
    stepCarousel(1);
    startCarouselAutoplay();
  };
}

function bindAuthEvents() {
  el("authTabLogin").onclick = () => setAuthMode("login");
  el("authTabSignup").onclick = () => setAuthMode("signup");
  el("authSubmit").onclick = () => {
    if (state.authMode === "signup") {
      signUpFromAuth();
      return;
    }
    loginFromAuth();
  };
}

function bindProfileEvents() {
  el("logOutTop").onclick = logOut;
  el("registerAgent").onclick = registerProfile;
}

function bindAppEvents() {
  el("logOutTop").onclick = logOut;
  el("loadDeck").onclick = loadProfiles;
  el("prevProfile").onclick = previousProfile;
  el("nextProfile").onclick = nextProfile;
  el("profilePicker").onchange = jumpToProfile;
  el("passBtn").onclick = passProfile;
  el("likeBtn").onclick = likeProfile;
  el("refreshInbox").onclick = refreshInbox;
  el("refreshConvos").onclick = refreshConversations;
  el("loadMessages").onclick = loadMessages;
  el("sendMessage").onclick = sendMessage;
  el("submitDecision").onclick = submitDecision;
  el("viewContact").onclick = viewContact;
  el("agentSend").onclick = sendAgentMessage;
  el("agentClear").onclick = clearAgentChat;
  el("agentInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      sendAgentMessage();
    }
  });

  const mobileLoad = el("mobileLoadProfiles");
  const mobileInbox = el("mobileRefreshInbox");
  const mobileSend = el("mobileSendMessage");
  if (mobileLoad) {
    mobileLoad.onclick = () => {
      scrollToModule("discover");
      loadProfiles();
    };
  }
  if (mobileInbox) {
    mobileInbox.onclick = () => {
      scrollToModule("inbox");
      refreshInbox();
    };
  }
  if (mobileSend) {
    mobileSend.onclick = () => {
      scrollToModule("chat");
      focusMessageComposer();
      sendMessage();
    };
  }
}

async function bootLanding() {
  renderCarousel();
  bindLandingEvents();
  startCarouselAutoplay();
}

async function bootAuth() {
  const user = await loadSession();
  if (user?.has_profile) {
    window.location.assign("/app");
    return;
  }
  if (user) {
    window.location.assign("/profile");
    return;
  }

  const mode = new URLSearchParams(window.location.search).get("mode");
  setAuthMode(mode === "signup" ? "signup" : "login");
  setAuthStatus("Not logged in.");
  bindAuthEvents();
}

async function bootProfile() {
  const user = await loadSession();
  if (!user) {
    window.location.assign("/auth?mode=login");
    return;
  }
  if (user.has_profile) {
    window.location.assign("/app");
    return;
  }

  bindProfileEvents();
}

async function bootApp() {
  const user = await loadSession();
  if (!user) {
    window.location.assign("/auth?mode=login");
    return;
  }
  if (!user.has_profile) {
    window.location.assign("/profile");
    return;
  }

  bindAppEvents();
  renderDeck();
  setNextStep("Start by loading profiles in Discover or checking your Inbox.");
  setFlowStep("discover");
  setDecisionControls();
  refreshInbox();
  refreshConversations();
  loadAgentChatHistory();
  log("Dashboard ready.");
}

function boot() {
  if (page === "landing") {
    bootLanding();
    return;
  }
  if (page === "auth") {
    bootAuth();
    return;
  }
  if (page === "profile") {
    bootProfile();
    return;
  }
  if (page === "app") {
    bootApp();
  }
}

boot();
