import type { NextConfig } from "next";

// In Docker, the nginx gateway routes /api to the backend, so the web app never sees it.
// For `npm run dev`, set API_URL (e.g. http://localhost:8080/api) to proxy /api there.
const apiUrl = process.env.API_URL;

const nextConfig: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  async rewrites() {
    return apiUrl ? [{ source: "/api/:path*", destination: `${apiUrl}/:path*` }] : [];
  },
};

export default nextConfig;
