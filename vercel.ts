const configuredOrigin = process.env.CORE_PROXY_URL;
if (!configuredOrigin)
  throw new Error("Set CORE_PROXY_URL to the HTTPS Render Core origin");
const core = new URL(configuredOrigin);
if (
  core.protocol !== "https:" ||
  core.username ||
  core.password ||
  core.pathname !== "/" ||
  core.search ||
  core.hash
)
  throw new Error("CORE_PROXY_URL must be a bare HTTPS origin");

// Keep auth and browser requests on the web origin so the host-only refresh cookie works.
export const config = {
  framework: null,
  installCommand: "npm --prefix apps/web ci",
  buildCommand: "VITE_API_URL=/ npm --prefix apps/web run build",
  outputDirectory: "apps/web/dist",
  rewrites: [
    { source: "/api/:path*", destination: `${core.origin}/api/:path*` },
    { source: "/v1/upload", destination: `${core.origin}/v1/upload` },
    { source: "/:path((?!assets/).*)", destination: "/index.html" },
  ],
  headers: ["/api/:path*", "/v1/upload"].map((source) => ({
    source,
    headers: [{ key: "Cache-Control", value: "private, no-store" }],
  })),
};
