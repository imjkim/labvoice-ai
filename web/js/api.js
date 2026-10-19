// Thin client for the VoiceOps JSON API. Every request carries the demo identity (X-User).

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export function createApi(getUserId) {
  async function request(path, { method = "GET", json, form, signal } = {}) {
    const headers = { "X-User": getUserId() };
    let body;
    if (json !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(json);
    } else if (form) {
      body = form; // the browser sets the multipart boundary
    }
    let res;
    try {
      res = await fetch(`/api${path}`, { method, headers, body, signal });
    } catch (err) {
      if (err.name === "AbortError") throw err;
      throw new ApiError(0, "Can't reach the VoiceOps server. Is it running?");
    }
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      const detail = data && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
      throw new ApiError(res.status, detail || `Request failed (${res.status})`);
    }
    return data;
  }

  return {
    health: () => request("/health"),
    users: () => request("/users"),
    ask: (message, sessionId, signal) =>
      request("/agent", { method: "POST", json: { message, session_id: sessionId }, signal }),
    voice: (blob, filename, sessionId, signal) => {
      const form = new FormData();
      form.append("audio", blob, filename);
      if (sessionId) form.append("session_id", sessionId);
      return request("/voice", { method: "POST", form, signal });
    },
    actions: () => request("/actions"),
    decide: (actionId, decision) => request(`/actions/${actionId}`, { method: "POST", json: { decision } }),
  };
}
