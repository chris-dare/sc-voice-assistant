// Product analytics (PostHog). Events carry ids, timings and device facts only:
// never transcripts, message text, file names or audio. Session replay masks all text and inputs.
let enabled = false;
const queue = [];

function deviceProps() {
  const ua = navigator.userAgent;
  const inApp = /FBAN|FBAV|Instagram|Line\/|Slack|LinkedInApp|Twitter|WhatsApp|GSA\/|; wv\)/i.test(ua);
  const ios = /iPad|iPhone|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  return {
    app: "sc-voice-poc",
    is_ios: ios,
    is_android: /Android/i.test(ua),
    in_app_browser: inApp,
    standalone: window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true,
    touch: window.matchMedia("(pointer: coarse)").matches,
    prompt_supported: typeof window.prompt === "function",
  };
}

export function initAnalytics(cfg) {
  const ph = cfg && cfg.posthog;
  if (!ph || !ph.token || enabled) return;
  // Official PostHog loader snippet (loads posthog-js from the regional assets host).
  /* eslint-disable */
  !function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.crossOrigin="anonymous",p.async=!0,p.src=s.api_host.replace(".i.posthog.com","-assets.i.posthog.com")+"/static/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="init capture register register_once register_for_session unregister identify alias people.set people.set_once set_config reset opt_in_capturing opt_out_capturing has_opted_out_capturing get_distinct_id getFeatureFlag isFeatureEnabled reloadFeatureFlags onFeatureFlags startSessionRecording stopSessionRecording".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);
  /* eslint-enable */
  window.posthog.init(ph.token, {
    api_host: ph.api_host,
    person_profiles: "identified_only",
    capture_pageview: true,
    capture_pageleave: true,
    autocapture: { dom_event_allowlist: ["click"], element_allowlist: ["button", "a"] },
    mask_all_text: true,
    mask_all_element_attributes: true,
    session_recording: { maskAllInputs: true, maskTextSelector: "*" },
    disable_surveys: true,
  });
  const version = document.querySelector('meta[name="app-version"]');
  window.posthog.register({ ...deviceProps(), app_version: version ? version.content : "unknown" });
  enabled = true;
  while (queue.length) { const [e, p] = queue.shift(); track(e, p); }
}

export function identify(userId) {
  try { if (enabled && userId) window.posthog.identify(userId); } catch {}
}

export function track(event, props = {}) {
  try {
    if (!enabled) { if (queue.length < 50) queue.push([event, props]); return; }
    window.posthog.capture(event, props);
  } catch {}
}
