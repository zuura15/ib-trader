/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        chrome: {
          950: "#090b0f",
          900: "#10141b",
          850: "#131922",
          800: "#18212d",
          700: "#223041",
          600: "#31455d"
        },
        accent: {
          blue: "#67b7ff",
          green: "#44d89e",
          amber: "#f7bd5c",
          red: "#ff6b6b"
        }
      },
      fontFamily: {
        sans: ["'IBM Plex Sans'", "system-ui", "sans-serif"],
        mono: ["'IBM Plex Mono'", "ui-monospace", "monospace"]
      },
      boxShadow: {
        panel: "0 0 0 1px rgba(148, 163, 184, 0.08), 0 8px 24px rgba(0, 0, 0, 0.35)"
      }
    }
  },
  plugins: []
};
