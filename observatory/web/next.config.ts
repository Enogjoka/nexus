import type { NextConfig } from "next";

// A fully static build: `next build` writes out/, which is copied to
// observatory/static and served by observatory/api.py. No Node server runs in
// production, and nothing here may add a runtime call to a third party.
const nextConfig: NextConfig = {
  output: "export",
  // /agents is emitted as agents.html and served for the extension-less path.
  trailingSlash: false,
  images: { unoptimized: true },
  reactStrictMode: true,
  productionBrowserSourceMaps: false,
};

export default nextConfig;
