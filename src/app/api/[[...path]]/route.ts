/** Server-only, uncached API transport. FastAPI owns authentication and CSRF. */
const noStore = "private, no-store, max-age=0, must-revalidate";
const requestHeaders = [
  "accept", "content-type", "cookie", "origin", "sec-fetch-site",
  "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user",
];
const hopHeaders = [
  "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
  "te", "trailer", "transfer-encoding", "upgrade",
];

function backendOrigin(): URL {
  const origin = new URL(
    process.env.POKEFOLIO_BACKEND_ORIGIN ?? "http://127.0.0.1:8000",
  );
  if (
    !["http:", "https:"].includes(origin.protocol) || origin.username ||
    origin.password || origin.pathname !== "/" || origin.search || origin.hash
  ) {
    throw new Error("Invalid backend origin configuration");
  }
  return origin;
}

async function proxy(request: Request): Promise<Response> {
  // Read request-time data outside the error handler so Next.js can correctly
  // detect this route as dynamic with Cache Components enabled.
  const incoming = new URL(request.url);
  try {
    const backend = backendOrigin();
    // Assign pathname instead of resolving an untrusted URL against the backend:
    // a path beginning with // must never select a different upstream host.
    const target = new URL(backend);
    target.pathname = incoming.pathname.slice("/api".length) || "/";
    target.search = incoming.search;
    const headers = new Headers();
    for (const name of requestHeaders) {
      const value = request.headers.get(name);
      if (value !== null) headers.set(name, value);
    }
    // Do not generate or forward any client-identity headers. Origin and Fetch
    // Metadata are copied as supplied, including absence; never synthesize trust.
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : await request.arrayBuffer(),
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(30_000)]),
    });
    const responseHeaders = new Headers(upstream.headers);
    const connectionHeaders = (upstream.headers.get("connection") ?? "").split(",");
    for (const name of [...hopHeaders, ...connectionHeaders]) {
      if (name.trim()) responseHeaders.delete(name.trim());
    }
    // fetch decodes the upstream body; its original length/encoding no longer apply.
    responseHeaders.delete("content-encoding");
    responseHeaders.delete("content-length");
    responseHeaders.delete("set-cookie");
    for (const cookie of upstream.headers.getSetCookie()) {
      responseHeaders.append("set-cookie", cookie);
    }
    // Keep FastAPI slash redirects on the browser-facing /api namespace.
    const location = responseHeaders.get("location");
    if (location) {
      const redirect = new URL(location, target);
      if (redirect.origin === backend.origin) {
        responseHeaders.set("location", `/api${redirect.pathname}${redirect.search}${redirect.hash}`);
      }
    }
    responseHeaders.set("cache-control", noStore);
    return new Response(upstream.body, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch {
    // Do not log request headers, bodies, cookies, or upstream error details.
    return Response.json(
      { detail: "API unavailable" },
      { status: 502, headers: { "Cache-Control": noStore } },
    );
  }
}

export { proxy as GET, proxy as HEAD, proxy as POST, proxy as PUT, proxy as PATCH,
  proxy as DELETE, proxy as OPTIONS };
