import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Allows the integration runner to use an isolated build directory.
  distDir: process.env.POKEFOLIO_NEXT_DIST_DIR ?? ".next",
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
